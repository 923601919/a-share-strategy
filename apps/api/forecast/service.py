"""盘后作业编排：结算 → 补结算 → 为下一个交易日起卦（自 tianji `app/src/jobs.ts` +
`core/src/service/daily.ts` 移植）。

作业的产出只有一样东西：**准确率证据**。因此里面对"出错时才会体现出来"的判断
都写在这里，而不是散在 CLI / API 里：

  · 时钟只有服务器时钟一个来源（没有 `--now` 等价物），作业跑的就是"现在"；
  · 数据源降级与口径要**逐条记下来**（静默换源等于让准确率的口径悄悄漂移）；
  · 当日行情拿不到时**留待结算**，不算未命中、也不算命中；
  · 起卦当天没有 K 线就不出记录（依据无法复核的记录不如没有）；
  · 每次运行都留痕，漏跑的交易日由缺口列表如实列出，**不伪造补跑**。

★ 一个实测出来的时序事实（2026-09-17 实测，本机）★

新浪指数日线在当日 16:49 仍未发布当天那根 bar（东财接口在本机被代理挡住，
降级到新浪）。所以 15:45 的作业**通常拿不到当日官方日线**。两条应对都写进了
代码，而不是靠"应该能拿到"：

  1. 当日用**指数实时行情**（现价/昨收）先结掉当天，`source='sina_rt'`；
  2. 次日官方日线到位后，结算记录被 **upsert 修正**，`source` 变成 `em`/`sina`。
     ——实际方向本就该跟着数据源走；把基于旧行情的「命中」永久冻住，
     比允许修正危险得多。

⚠ 第 2 条曾经是**假的**（代码写着、却执行不到）：修正判断被放在"待结算"循环里，
而实时结算恰恰写入了 outcome、使该记录离开了待结算队列。
现在修正走独立的复核通道（`_reconcile_realtime`），见 `settle_pending` 的说明。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import settings

from . import change as chg
from . import stats as st
from .calendar_ctx import build_divination_context
from .engine import DivineRequest, divine
from .profile import (
    CAST_HOUR,
    CAST_MINUTE,
    ENGINE_VERSION,
    MARKET_INDEX_CODE,
    RULESET_VERSION,
    build_profile,
    canonicalize,
    hash_profile,
)
from .seed import sha256_hex
from .util import js_round

logger = logging.getLogger("forecast")

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 行情同步的默认回看天数：足够覆盖"作业停了几个月后重启"仍能补齐结算
DEFAULT_SYNC_DAYS = 400

# 结算窗口的**基准回看天数**。
#
# ★ 为什么需要它（这是一个真实踩过的 off-by-one）★
#
# 判定"某日涨跌"要用它**前一根** bar 作基准（见 change.change_at：index 必须 ≥ 1）。
# 若把查询窗口的起点直接设成"最早的待结算日"，那根基准 bar 就落在窗口外，
# 于是 `change_at(bars, 0)` 返回 None → **最早的待结算日永远结不掉**。
# 它不会报错，只会一天天堆在待结算队列里，而界面上看起来只是"样本还少"。
#
# 取 20 个自然日：A 股最长假期（春节/国庆）连休约 9~10 个自然日，
# 留一倍余量；这个查询走本地 SQLite，放宽的成本可以忽略。
SETTLE_BASE_LOOKBACK_DAYS = 20


def now_shanghai() -> datetime:
    """服务器当前时刻（北京时间）。作业唯一允许的时间来源。"""
    return datetime.now(SHANGHAI)


def symbol_of(settings_obj: Any = None) -> str:
    s = settings_obj or settings
    return str(getattr(s, "market_forecast_symbol", "000001.SH")).strip() or "000001.SH"


def profile_of(settings_obj: Any = None) -> dict:
    s = settings_obj or settings
    salt = str(getattr(s, "market_forecast_seed_salt", "") or "").strip()
    from .profile import DEFAULT_SEED_SALT

    return build_profile(salt or DEFAULT_SEED_SALT)


def flat_threshold_pct(settings_obj: Any = None) -> float:
    s = settings_obj or settings
    return float(getattr(s, "market_forecast_flat_threshold_pct", 0.05))


def _content_hash(rec: dict) -> str:
    """记录内容哈希（除自身外全字段 canonical 化后 sha256）。

    触发器挡住了 UPDATE/DELETE，但它挡不住"绕过 SQLite 直接改文件"。有了这个
    哈希，读取时就能复核一条记录是否被手改过：改了字段就对不上哈希。
    """
    payload = {k: v for k, v in rec.items() if k not in ("content_hash",)}
    return sha256_hex(canonicalize(payload))


def _prediction_record(pred, symbol: str, now_iso: str) -> dict:
    rec = {
        "id": pred.id,
        "symbol": symbol,
        "target_date": pred.target_date,
        "as_of": pred.as_of,
        "generated_at": pred.generated_at,
        "origin": pred.origin,
        "direction": pred.direction,
        "strength": pred.strength,
        "basis_json": json.dumps(pred.basis, ensure_ascii=False, sort_keys=True),
        "profile_hash": pred.profile_hash,
        "engine_ver": pred.engine_version,
        "ruleset_ver": pred.ruleset_version,
        "created_at": now_iso,
    }
    rec["content_hash"] = _content_hash(rec)
    return rec


def _code_of(symbol: str) -> str:
    """'000001.SH' → '000001'。"""
    head = symbol.split(".")[0].strip()
    return head.zfill(6)


# ================================================================ 行情同步


def sync_index_daily(symbol: str, from_date: date, to_date: date) -> dict:
    """同步指数日线到库。行情是**可修正**数据，这里用 upsert。"""
    from db import upsert_market_index_daily
    from providers.akshare_client import fetch_index_daily

    days = max(30, (to_date - from_date).days + 5)
    bars = fetch_index_daily(_code_of(symbol), limit=min(1200, days))
    kept = [b for b in bars if from_date.isoformat() <= b["date"] <= to_date.isoformat()]
    written = upsert_market_index_daily(symbol, kept)
    sources = sorted({b["source"] for b in kept})
    return {"written": written, "sources": sources, "last_date": kept[-1]["date"] if kept else None}


# ================================================================ 结算


# 实时口径的 source 标记。这些结算值**允许**被官方日线修正——
# 它们的依据是盘中价，不是收盘价。
REALTIME_SOURCES = ("sina_rt", "rtQuote")


def settle_pending(
    symbol: str,
    up_to: date,
    today: date | None = None,
    *,
    allow_realtime: bool = True,
) -> dict:
    """结算所有到期但仍未结算的 live 预测，并复核此前用实时口径结掉的样本。
    幂等：可以随便重跑。

    目标日拿不到行情 → **保持待结算**（不计入分母，也不记未命中），
    下一次作业自然补上。这是"没拿到"与"没有"必须能被分辨的那条线。

    ★ 为什么"复核实时口径"要单列一遍（而不是顺手写在下面的循环里）★

    最初的写法把修正判断放在 pending 循环内部（`existing = get_...outcome(...)`）。
    那段代码**永远执行不到**：`list_pending_settlement` 的定义就是「没有 outcome
    的记录」，而实时口径结算恰恰写入了 outcome —— 于是这条记录立刻离开了待结算
    队列，第二次作业根本看不到它。结果就是文件头写的「次日官方日线到位后 upsert
    修正」这条机制完全失效，而代码看起来是实现了的。

    所以复核必须**单独扫一遍已有 outcome 里 source 属于实时口径的那些**。
    这也解释了 `corrections` 与 `confirmed` 的区别：
      · corrections —— 官方口径与实时口径**数值不同**，实际方向被改了；
      · confirmed   —— 数值一致，只是把 source 从实时口径换成官方日线，
                       从此不再重复复核。
    把两者混为一谈会让"修正了多少条"这个数字失去含义。
    """
    from db import (
        list_market_forecast_outcomes,
        list_market_index_daily,
        list_pending_settlement,
        upsert_market_forecast_outcome,
    )

    today = today or now_shanghai().date()
    flat = flat_threshold_pct()
    up_to_iso = up_to.isoformat()
    pending = list_pending_settlement(symbol, up_to_iso)
    realtime_settled = [
        o
        for o in list_market_forecast_outcomes(symbol, limit=2000)
        if o.get("source") in REALTIME_SOURCES and o["target_date"] <= up_to_iso
    ]

    if not pending and not realtime_settled:
        return {"settled": [], "pending": [], "corrections": [], "confirmed": [], "checked": 0}

    earliest = min([p["target_date"] for p in pending] + [o["target_date"] for o in realtime_settled])
    # ★ 必须向前多取一天（这里取 20 天更稳）：判涨跌要用前一根 bar 作基准。
    #   少这一天，最早的待结算日会永远卡在队列里且不报错。见常量处的说明。
    start = (date.fromisoformat(earliest) - timedelta(days=SETTLE_BASE_LOOKBACK_DAYS)).isoformat()
    bars = [
        chg.DailyBar(date=b["trade_date"], close=b["close"], prev_close=b["prev_close"])
        for b in list_market_index_daily(symbol, start=start, end=up_to_iso)
    ]
    idx = chg.index_by_date(bars)

    # 当日官方日线可能还没发布 → 备一条实时口径（只对"今天"有效）
    rt = _realtime_judgement(symbol, flat) if allow_realtime else None

    settled: list[str] = []
    still_pending: list[str] = []

    for p in pending:
        target = p["target_date"]
        judgement = chg.judge_outcome(symbol, target, bars, idx, flat)
        source = "bar"
        if judgement is None and rt is not None and rt["date"] == target:
            judgement = rt["judgement"]
            source = "sina_rt"
        if judgement is None:
            still_pending.append(target)
            continue

        # 这里不需要检查"是否已有 outcome"：pending 的定义就是没有 outcome 的记录。
        upsert_market_forecast_outcome(
            {
                "symbol": symbol,
                "target_date": target,
                "base_date": judgement.base_date,
                "base_close": judgement.base_close,
                "close": judgement.close,
                "actual_change": judgement.actual_change,
                "actual_direction": judgement.actual_direction,
                "basis": judgement.basis,
                "source": source,
                "ruleset_ver": p.get("ruleset_ver") or RULESET_VERSION,
                "settled_at": now_shanghai().isoformat(),
            }
        )
        settled.append(target)

    corrections, confirmed = _reconcile_realtime(
        symbol, realtime_settled, bars, idx, flat, upsert_market_forecast_outcome
    )

    return {
        "settled": settled,
        "pending": still_pending,
        "corrections": corrections,
        "confirmed": confirmed,
        "checked": len(pending) + len(realtime_settled),
    }


def _reconcile_realtime(
    symbol: str,
    realtime_settled: list[dict],
    bars: list,
    idx: dict,
    flat: float,
    upsert,
) -> tuple[list[str], list[str]]:
    """用官方日线复核此前实时口径结掉的样本。返回 (corrections, confirmed)。

    官方日线还没到 → 原样保留实时口径（**不是**清空、也不是记成未命中）：
    它是一条真结算，只是依据弱一些。
    """
    corrections: list[str] = []
    confirmed: list[str] = []
    now_iso = now_shanghai().isoformat()

    for o in realtime_settled:
        target = o["target_date"]
        j = chg.judge_outcome(symbol, target, bars, idx, flat)
        if j is None:
            continue

        changed = (
            j.close != o["close"]
            or j.base_close != o["base_close"]
            or j.actual_direction != o["actual_direction"]
        )
        upsert(
            {
                "symbol": symbol,
                "target_date": target,
                "base_date": j.base_date,
                "base_close": j.base_close,
                "close": j.close,
                "actual_change": j.actual_change,
                "actual_direction": j.actual_direction,
                "basis": j.basis,
                "source": "bar",
                "ruleset_ver": o.get("ruleset_ver") or RULESET_VERSION,
                "settled_at": now_iso,
            }
        )
        (corrections if changed else confirmed).append(target)

    return corrections, confirmed


def _realtime_judgement(symbol: str, flat: float) -> dict | None:
    """当日实时口径的结算依据（现价=当日收盘，昨收=前收盘）。

    失败时返回 None —— 由调用方把该样本留在待结算，不猜。
    """
    from providers.akshare_client import fetch_index_quotes

    code6 = _code_of(symbol)
    try:
        quotes = fetch_index_quotes([f"sh{code6}"])
    except Exception as e:  # noqa: BLE001
        logger.warning("index realtime quote failed: %s", e)
        return None
    q = quotes.get(f"sh{code6}") or {}
    price = float(q.get("price") or 0)
    pre = float(q.get("pre_close") or 0)
    if price <= 0 or pre <= 0:
        return None
    try:
        ratio, direction = chg.judge_close_pair(pre, price, flat)
    except ValueError:
        return None
    today = now_shanghai().date()
    # 实时行情只能代表"今天收盘后"的那个时点。收盘前取到的是盘中价，
    # 用它结算等于拿未完成的行情冒充收盘价——所以只允许 15:00 之后使用。
    now = now_shanghai()
    if now.hour < CAST_HOUR:
        return None
    from .change import OutcomeJudgement

    return {
        "date": today.isoformat(),
        "judgement": OutcomeJudgement(
            symbol=symbol,
            target_date=today.isoformat(),
            base_date="实时前收盘",
            base_close=pre,
            close=price,
            actual_change=ratio,
            actual_direction=direction,
            basis="rtQuote",
            flat_threshold_pct=flat,
        ),
    }


# ================================================================ 起卦


def generate_prediction(
    symbol: str,
    target_date: date,
    as_of: datetime,
    profile: dict,
    origin: str = "live",
) -> dict | None:
    """为指定目标日产出并落库一条预测。已存在同口径记录时返回 None（不覆盖）。"""
    from db import has_live_prediction_for, insert_market_prediction

    if origin == "live":
        existing = has_live_prediction_for(symbol, target_date.isoformat())
        if existing is not None:
            return None

    as_of_iso = as_of.isoformat()
    ctx = build_divination_context(
        as_of.date().isoformat(),
        hour=CAST_HOUR,
        minute=CAST_MINUTE,
        late_zi_hour=profile.get("lateZiHour", "nextDay"),
    )
    now_iso = now_shanghai().isoformat()
    pred = divine(
        DivineRequest(
            symbol=symbol,
            target_date=target_date.isoformat(),
            as_of=as_of_iso,
            method="liuyao",
            mode="deterministic",
            profile=profile,
            context=ctx,
            engine_version=ENGINE_VERSION,
            ruleset_version=RULESET_VERSION,
            profile_hash=hash_profile(profile),
            now=now_iso,
            origin=origin,
        )
    )
    rec = _prediction_record(pred, symbol, now_iso)
    created = insert_market_prediction(rec)
    return {"id": pred.id, "created": created, "record": rec, "prediction": pred}


# ================================================================ 作业主体


def run_daily_forecast(
    *,
    now: datetime | None = None,
    today: date | None = None,
    symbol: str | None = None,
) -> dict:
    """跑一次盘后作业：同步行情 → 结算 → 起卦下一个交易日 → 留痕。

    任何一步失败都不影响其它步：结算的成果不该被起卦的失败吞掉，反之亦然。
    """
    from db import save_market_forecast_run
    from services.trade_calendar import is_trade_date, nth_trade_date_after

    started = now or now_shanghai()
    today = today or started.date()
    symbol = symbol or symbol_of()
    profile = profile_of()

    result: dict[str, Any] = {
        "run_date": today.isoformat(),
        "trade_date": is_trade_date(today),
        "symbol": symbol,
        "synced": None,
        "settlement": None,
        "created": None,
        "target_date": None,
        "status": "ok",
        "notes": [],
    }

    if not result["trade_date"]:
        result["status"] = "holiday"
        result["notes"].append("非交易日，跳过（这本来就该什么都没做）")
        save_market_forecast_run(today.isoformat(), "holiday", result["notes"][0], started.isoformat())
        return result

    # ── ① 同步行情（含一段回看，覆盖"作业停过几天"）──
    try:
        result["synced"] = sync_index_daily(
            symbol, today - timedelta(days=DEFAULT_SYNC_DAYS), today
        )
    except Exception as e:  # noqa: BLE001
        result["notes"].append(f"行情同步失败：{e}")
        result["status"] = "partial"
        logger.warning("forecast sync failed: %s", e)

    # ── ② 结算（到期未结算的全部 + 复核此前的实时口径）──
    try:
        result["settlement"] = settle_pending(symbol, up_to=today, today=today)
        stl = result["settlement"]
        if stl["pending"]:
            result["notes"].append(
                f"待结算 {len(stl['pending'])} 条"
                f"（{', '.join(stl['pending'][:5])}）："
                "目标日行情未发布或缺失，留在待结算等下一次作业补——"
                "既不算命中也不算未命中。"
            )
        if stl["corrections"]:
            result["notes"].append(
                f"官方日线修正了 {len(stl['corrections'])} 条实时口径结算"
                f"（{', '.join(stl['corrections'][:5])}）："
                "实际方向已按官方口径更新；实时口径只是「先结掉」，不是最终值。"
            )
    except Exception as e:  # noqa: BLE001
        result["notes"].append(f"结算失败：{e}")
        result["status"] = "partial"
        logger.exception("forecast settle failed")

    # ── ③ 起卦：下一个交易日 ──
    try:
        target = nth_trade_date_after(today, 1)
        if target is None:
            result["notes"].append("算不出下一个交易日（交易日历数据越界），本次不起卦。")
            result["status"] = "partial"
        else:
            result["target_date"] = target.isoformat()
            created = generate_prediction(
                symbol,
                target,
                # 起卦时点 = 收盘时刻。它必须早于目标日，否则引擎会拒绝。
                as_of=datetime.combine(today, datetime.min.time(), tzinfo=SHANGHAI).replace(
                    hour=CAST_HOUR, minute=CAST_MINUTE
                ),
                profile=profile,
            )
            if created is None:
                result["notes"].append(
                    f"目标日 {result['target_date']} 已存在同口径的 live 记录，"
                    "本次未重复生成。预测写入即冻结，重跑作业不会覆盖它。"
                )
            else:
                result["created"] = created["id"]
    except Exception as e:  # noqa: BLE001
        result["notes"].append(f"起卦失败：{e}")
        result["status"] = "partial"
        logger.exception("forecast divine failed")

    # ── ④ 出口判定：目标日到底有没有 live 预测 ──
    #
    # ★ 这里刻意用「状态」而不是「本轮动作」来判定成败 ★
    #
    # 最初的写法是 `if result["created"] is None and not result["notes"]: failed`。
    # 那个分支**永远不可达**：`created is None` 的两条来路（已存在同口径记录、
    # 算不出下一交易日）都会先 append 一条 note，条件里的 `not notes` 因此恒假。
    # 后果是起卦环节静默坏掉时，作业照样把当天记成 ok —— 一个"漏了一天样本"
    # 的历史，看起来和正常历史一模一样。
    #
    # 换成状态式判据后：作业跑完必须存在「目标日的 live 预测」这条事实。
    # 它与「是不是这一轮创建的」无关（重跑作业不会误报失败），
    # 但它能揪出所有真正该报警的情形——起卦抛错、UTC 越界、日历越界、库写不进去。
    from db import has_live_prediction_for

    target_iso = result.get("target_date")
    if target_iso is None:
        result["status"] = "failed"
        result["notes"].append(
            "作业结束但没有定出目标交易日（交易日历越界），本轮没有任何产出。"
        )
    elif has_live_prediction_for(symbol, target_iso) is None:
        result["status"] = "failed"
        result["notes"].append(
            f"作业结束但目标日 {target_iso} 仍无 live 预测记录。"
            "这是失败而不是「今天没事」——少掉的每一天都会让准确率样本静悄悄地缺一段。"
        )

    save_market_forecast_run(
        today.isoformat(),
        result["status"],
        "；".join(result["notes"]),
        started.isoformat(),
    )
    logger.info(
        "forecast run %s: status=%s created=%s settlement=%s",
        today.isoformat(),
        result["status"],
        result["created"],
        result["settlement"],
    )
    return result


# ================================================================ 回放（P4）


def backfill_replay(symbol: str, target_dates: list[str], as_of_map: dict[str, str]) -> dict:
    """回补历史预测，**全部标记 origin='replay'**。

    ⚠ replay 记录不进准确率（统计只取 live）：它证明不了任何事，因为
    「起卦时点」是我们事后挑的。它能做的只有一件事——让页面在功能刚上线时
    不是一片空白，并且能被拿来检查链路。

    `as_of_map` 是 `target_date → 起卦日` 的映射，**必须显式给出**：
    让调用方为每条记录挑一个起卦时点，就等于允许"挑一个能摇出好卦的日子"，
    而那正是这个功能最不能做的事。调用方应传"目标日的上一个交易日"。
    """
    profile = profile_of()
    out = {"created": [], "skipped": [], "failed": []}
    for td in target_dates:
        as_of_date = as_of_map.get(td)
        if not as_of_date:
            out["failed"].append({"target_date": td, "reason": "缺少起卦日（as_of）"})
            continue
        try:
            target = date.fromisoformat(td)
            as_of = datetime.combine(
                date.fromisoformat(as_of_date), datetime.min.time(), tzinfo=SHANGHAI
            ).replace(hour=CAST_HOUR, minute=CAST_MINUTE)
            created = generate_prediction(symbol, target, as_of=as_of, profile=profile, origin="replay")
            if created is None:
                out["skipped"].append(td)
            else:
                out["created"].append(td)
        except Exception as e:  # noqa: BLE001
            out["failed"].append({"target_date": td, "reason": str(e)})
    return out


# ================================================================ 读取用载荷


def _with_outcome(pred_rows: list[dict], symbol: str) -> list[dict]:
    from db import list_market_forecast_outcomes

    outcomes = {o["target_date"]: o for o in list_market_forecast_outcomes(symbol, limit=2000)}
    out = []
    for p in pred_rows:
        o = outcomes.get(p["target_date"])
        hit = chg.hit_of(p["direction"], o["actual_direction"]) if o else None
        out.append(
            {
                "id": p["id"],
                "symbol": p["symbol"],
                "target_date": p["target_date"],
                # as_of 与 generated_at 是「这条预测是不是事前产出的」的直接证据，
                # 列表里必须带上：没有它，用户只能靠 target_date 自己猜。
                "as_of": p["as_of"],
                "generated_at": p["generated_at"],
                "origin": p["origin"],
                "direction": p["direction"],
                "strength": p["strength"],
                "hexagram": (p.get("basis") or {}).get("hexagram"),
                "changed_hexagram": (p.get("basis") or {}).get("changed_hexagram"),
                "engine_ver": p["engine_ver"],
                "ruleset_ver": p["ruleset_ver"],
                "outcome": (
                    {
                        "actual_direction": o["actual_direction"],
                        "actual_change": o["actual_change"],
                        "close": o["close"],
                        "base_close": o["base_close"],
                        "basis": o["basis"],
                        "source": o["source"],
                        "hit": hit,
                        "settled_at": o["settled_at"],
                    }
                    if o
                    else None
                ),
            }
        )
    return out


def latest_payload(symbol: str | None = None) -> dict:
    """当前生效的预测 + 最近一条已结算的预测。"""
    from db import list_market_predictions

    symbol = symbol or symbol_of()
    rows = _with_outcome(list_market_predictions(symbol=symbol, limit=60, origin="live"), symbol)
    today = now_shanghai().date().isoformat()

    upcoming = [r for r in rows if r["target_date"] >= today]
    latest = upcoming[0] if upcoming else None
    recent_settled = next((r for r in rows if r["outcome"] is not None), None)

    return {
        "symbol": symbol,
        "symbol_label": getattr(settings, "market_forecast_label", "上证指数"),
        "server_time": now_shanghai().isoformat(),
        "upcoming": latest,
        "recent_settled": recent_settled,
        "disclaimer": "仅供娱乐，不构成投资建议。",
    }


def history_payload(symbol: str | None = None, limit: int = 30) -> dict:
    from db import list_market_predictions

    symbol = symbol or symbol_of()
    rows = _with_outcome(
        list_market_predictions(symbol=symbol, limit=max(1, min(365, limit)), origin="live"), symbol
    )
    return {
        "symbol": symbol,
        "items": rows,
        "disclaimer": "仅供娱乐，不构成投资建议。",
    }


def accuracy_payload(symbol: str | None = None, window_days: int = 0) -> dict:
    """准确率报告。⚠ 不存在"只给百分比"的形态：报告结构里必然带基线、
    置信区间、p 值、门槛与样本充足性（见 stats.build_accuracy_report）。"""
    from db import list_market_predictions

    symbol = symbol or symbol_of()
    rows = list_market_predictions(symbol=symbol, limit=2000, origin="live")
    if window_days and window_days > 0:
        cutoff = (now_shanghai().date() - timedelta(days=window_days)).isoformat()
        rows = [r for r in rows if r["target_date"] >= cutoff]

    joined = _with_outcome(rows, symbol)
    outcomes = [
        {
            "direction": r["direction"],
            "actual_direction": r["outcome"]["actual_direction"] if r["outcome"] else None,
        }
        for r in joined
    ]
    report = st.build_accuracy_report(symbol, outcomes, window_days)

    # 偏置匹配置换检验：只在样本足够做检验时才给（平盘样本按口径剔除）
    pairs = [
        (r["direction"], r["outcome"]["actual_direction"])
        for r in joined
        if r["outcome"] and chg.hit_of(r["direction"], r["outcome"]["actual_direction"]) is not None
    ]
    report["permutation"] = None
    if len(pairs) >= 20:
        report["permutation"] = st.permutation_test(
            [p[0] for p in pairs], [p[1] for p in pairs]
        )
        p_perm = report["permutation"]["p_value"]
        report["note"] += (
            f" 偏置匹配置换检验：p = {p_perm}"
            f"（{report['permutation']['iterations']} 次重排，"
            f"零分布均值 {report['permutation']['null_mean']:.3f}，"
            f"观测 {report['permutation']['observed_rate']:.3f}）。"
        )
    report["pending_count"] = sum(1 for r in joined if r["outcome"] is None)
    report["disclaimer"] = "仅供娱乐，不构成投资建议。"
    return report


def runs_payload(symbol: str | None = None, limit: int = 30, gap_days: int = 60) -> dict:
    """作业运行记录 + **漏跑缺口列表**。

    缺口 = 交易日但没有任何预测覆盖的那一天。它必须被看见：一条"漏跑但看起来
    正常"的历史，会让准确率样本静悄悄地少掉几段。
    """
    from db import list_market_forecast_runs, list_market_forecast_target_dates
    from services.trade_calendar import is_trade_date

    symbol = symbol or symbol_of()
    covered = set(list_market_forecast_target_dates(symbol, origin="live"))

    today = now_shanghai().date()
    gaps: list[str] = []
    d = today - timedelta(days=max(1, min(365, gap_days)))
    while d < today:
        iso = d.isoformat()
        if is_trade_date(d) and iso not in covered:
            gaps.append(iso)
        d = d + timedelta(days=1)

    return {
        "symbol": symbol,
        "runs": list_market_forecast_runs(limit=max(1, min(200, limit))),
        "gaps": gaps,
        "gap_note": (
            "缺口是**如实列出**的：漏跑的那一天不会被伪造补跑（回补只能走 replay，"
            "而 replay 不进准确率）。"
        ),
        "disclaimer": "仅供娱乐，不构成投资建议。",
    }


def verify_content_hash(prediction_id: str) -> dict:
    """复核一条预测记录的 content_hash（用于事后审计手改库的场景）。"""
    from db import get_market_prediction

    row = get_market_prediction(prediction_id)
    if row is None:
        return {"found": False, "ok": False}
    rec = {
        k: v
        for k, v in row.items()
        if k
        in (
            "id", "symbol", "target_date", "as_of", "generated_at", "origin",
            "direction", "strength", "profile_hash", "engine_ver", "ruleset_ver", "created_at",
        )
    }
    rec["basis_json"] = json.dumps(row.get("basis") or {}, ensure_ascii=False, sort_keys=True)
    return {"found": True, "ok": _content_hash(rec) == row["content_hash"], "expected": row["content_hash"]}


def strength_of_score(score: float) -> float:
    return js_round(min(1.0, abs(score - 0.5) * 2), 3)


__all__ = [
    "accuracy_payload",
    "backfill_replay",
    "generate_prediction",
    "history_payload",
    "latest_payload",
    "now_shanghai",
    "run_daily_forecast",
    "runs_payload",
    "settle_pending",
    "sync_index_daily",
    "verify_content_hash",
]
