# 大盘涨跌预判 · 迁移设计（tianji → a-share-strategy）

> 状态：设计稿，未实现。源头：`D:\project\aiproject\tianji`（TypeScript monorepo）。
> 目标：把「预判上证指数次日涨跌 → 收盘结算 → 统计检验」整条链路迁入本项目后端，
> **纯服务端运行，不依赖 WorkBuddy**（无 agent、无 automation，调度走本项目已有的 APScheduler）。

---

## 0. 一句话

每个交易日收盘后，用确定性六爻起卦预判**下一个交易日**上证指数的涨跌方向，
写入即冻结；次日收盘后按实际行情结算；准确率永远连同置信区间、基线与显著性检验一起展示。
**仅供娱乐，不构成投资建议**（这条声明随功能一起迁移，进页面页脚与 API 返回）。

## 1. 迁移范围

### 迁入

| 能力 | tianji 源 | 说明 |
|---|---|---|
| 确定性起卦 | `core/src/forecast/seed.ts` + `internal/prng.ts` | 种子 = 标的+目标日+盐值，xoshiro128** PRNG |
| 起卦/装卦 | `core/src/liuyao/{cast,dress}.ts` + `data/{trigrams,hexagrams,najia,deities}.ts` | 纳甲、六亲、六神、世应、旬空、旺衰 |
| 断卦规则 | `core/src/forecast/rules.ts` | RuleConfig 权重 + reasoning 依据链 |
| 预测引擎 | `core/src/forecast/engine.ts` | 时间边界校验、Prediction 冻结记录 |
| 历法上下文 | `core/src/calendar` + `service/context.ts` | 年/月/日柱、旬空、月建五行 |
| 结算方向 | `core/src/market/change.ts` | 平盘阈值 0.05%、对称命中口径 |
| 准确率统计 | `core/src/stats/{accuracy,binomial}.ts` | Wilson CI、精确二项检验、基线 |
| 诚信护栏 | `engine.ts` / `jobs.ts` / `storage/ledger.ts` 中的设计 | 见 §6 清单 |
| 调度 | `app/src/{daily,jobs}.ts` 的盘后作业 | 改写为 APScheduler job |

### 明确不迁

- **LLM 解读**（interpret 模块）——本项目没有 LLM 层；
- **紫微 / 八字 / 排盘 UI**——与大盘预判无关；
- **多流派档案系统**——固定一个内置档案（`standard-cn`，seedSalt=`yz-standard-v1`），与 tianji 默认档案同值，保证两边可对账；
- **持仓个股预测**——只迁大盘 `000001.SH`；但 schema 保留 symbol 维度，将来要扩展不用改表；
- **偏置匹配置换检验**（backtest/permutation.ts）——二期可选，一期先保留「永远猜涨」基线与警示措辞；
- **东财→腾讯→新浪三源降级链**——本项目已有 akshare 双源（EM→新浪）惯例，降级与口径记录照搬思想，实现走现有 `providers/`；
- **hash 链账本**（predictions.journal.jsonl）——用「SQLite 只增触发器 + 记录内容 sha256」替代（理由见 §4.2）；
- **TS 交易日历**——本项目已有 `services/trade_calendar.py`（含 `nth_trade_date_after`，正好是「下一交易日」的取法）。

## 2. 源链路（tianji 现状，供对照）

```
盘后作业 (app/src/jobs.ts: runDailyOnce)
  ├─ 时钟护栏：--now 与系统时钟差 > 2h 拒绝运行（防伪造 live 记录）
  ├─ 数据源：东财 → 腾讯 → 新浪（降级逐条记录）
  ├─ 结算：judgeOutcomes() 用 K 线算实际方向（change.ts 的四级基准级联）
  ├─ 起卦：divine() 产出下个交易日的 Prediction（写入即冻结）
  └─ 账本校验：hash 链对不上 → 退出码 2，停手等人
预测记录 id = sha256(symbol|targetDate|profileHash|engineVersion|rulesetVersion|origin)[:16]
准确率 = 已结算 live 样本的命中率 + Wilson CI + 精确二项 p 值 + 永远猜涨基线
```

## 3. 目标架构

### 3.1 新包 `apps/api/forecast/`

| 文件 | 职责 | 对应 tianji |
|---|---|---|
| `prng.py` | xoshiro128** + splitmix32，逐位一致移植 | `internal/prng.ts` |
| `seed.py` | 种子材料 `SYMBOL\|TARGET\|SALT` 与派生 | `forecast/seed.ts` |
| `foundation.py` | 干支/五行/六冲六合常量与推算 | `foundation/{ganzhi,wuxing,relations}.ts` |
| `liuyao_data.py` | 八卦、64 卦、纳甲、六神表 | `liuyao/data/*.ts` |
| `cast.py` | 六爻值 → 卦象（含变/互/错/综卦） | `liuyao/cast.ts` |
| `dress.py` | 装卦：纳甲→六亲→六神→世应→旬空旺衰 | `liuyao/dress.ts` |
| `rules.py` | RuleConfig + evaluate + reasoning | `forecast/rules.ts` |
| `engine.py` | divine()：护栏 + 起卦 + 断卦 + Prediction | `forecast/engine.ts` |
| `calendar_ctx.py` | lunar_python → DivinationContext 桥接 | `calendar/` + `service/context.ts` |
| `change.py` | directionOf / hitOf（指数口径） | `market/change.ts` |
| `stats.py` | Wilson / 精确二项 / 准确率报告 | `stats/*.ts` |
| `service.py` | 盘后作业编排：结算→补结算→起卦次日 | `app/src/jobs.ts` + `core/src/service/daily.ts` |

### 3.2 历法依赖：`lunar-python`

tianji 的历法层唯一外部依赖是 `lunar-typescript`（6tail）。Python 侧用**同作者的
`lunar-python`**（纯 Python，无重量依赖），三处关键口径方法一一对应：

| 口径 | lunar-typescript | lunar-python |
|---|---|---|
| 年柱（立春为界） | `getYearInGanZhiExact()` | `getYearInGanZhiExact()` |
| 月柱（节气精确时刻为界） | `getMonthInGanZhiExact()` | `getMonthInGanZhiExact()` |
| 日柱 | `getDayInGanZhiExact()` | `getDayInGanZhiExact()` |

**真太阳时不迁**：已核对 `dress.ts` 与 `rules.ts`，断卦只消费
日柱干支、月柱（月建）、旬空、月建五行——**不使用时辰**。起卦固定在收盘后
（15:45），永不触及晚子时（23:00）与夏令时（1991 年后中国无夏令时）边界。
这条结论写成设计不变式，并由黄金向量测试钉住。

### 3.3 数据流时序

```
交易日 15:45  APScheduler job "market-forecast"（周一至周五）
  ├─ ① 同步指数日线（akshare EM→新浪，落 market_index_daily，可修正）
  ├─ ② 结算：所有 target_date ≤ 今天 且未结算/待复核的 live 预测
  │      （今天到不了行情就留在「待结算」，明天自然补上——幂等）
  ├─ ③ 起卦：target_date = nth_trade_date_after(今天, 1)
  │      as_of = 本次运行时刻（Asia/Shanghai 服务器时钟）
  │      已有同 id 记录 → 跳过（幂等，防调度重复合并）
  └─ ④ 写运行记录 market_forecast_runs（漏跑检测的对照表）
```

结算先于起卦：即使 ③ 失败，② 的成果不丢；反之亦然（各自独立 try/except，
任何一步失败记入 run 记录，不中断其他步）。

## 4. 数据库设计

### 4.1 DDL

```sql
-- 预测记录：写入即冻结，只允许 INSERT
CREATE TABLE IF NOT EXISTS market_predictions (
  id            TEXT PRIMARY KEY,      -- sha256(symbol|target_date|profile_hash|engine_ver|ruleset_ver|origin)[:16]
  symbol        TEXT NOT NULL,          -- '000001.SH'
  target_date   TEXT NOT NULL,          -- 目标交易日 ISO
  as_of         TEXT NOT NULL,          -- 起卦时点 ISO（必须 < target_date）
  generated_at  TEXT NOT NULL,
  origin        TEXT NOT NULL CHECK (origin IN ('live','replay')),
  direction     TEXT NOT NULL CHECK (direction IN ('up','down','flat')),
  strength      REAL NOT NULL,          -- 0–1
  basis_json    TEXT NOT NULL,          -- 卦象/用神/reasoning/种子材料，完整依据链
  profile_hash  TEXT NOT NULL,
  engine_ver    TEXT NOT NULL,
  ruleset_ver   TEXT NOT NULL,
  content_hash  TEXT NOT NULL           -- 除自身外全字段 canonical 化后的 sha256
);
CREATE INDEX IF NOT EXISTS idx_mp_target ON market_predictions(target_date);

-- 指数日线缓存：可修正（数据源改历史值时 upsert），非冻结
CREATE TABLE IF NOT EXISTS market_index_daily (
  symbol      TEXT NOT NULL,
  trade_date  TEXT NOT NULL,
  close       REAL NOT NULL,
  prev_close  REAL,                     -- 交易所口径缺失时为 NULL，结算时用前根 close
  source      TEXT NOT NULL,            -- 'em' / 'sina'
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

-- 结算结果：按 (symbol, target_date) upsert（数据源修正后可重结算），
-- 但 actual_direction 的口径（flat 阈值等）随 ruleset_ver 固化在行内
CREATE TABLE IF NOT EXISTS market_forecast_outcomes (
  symbol           TEXT NOT NULL,
  target_date      TEXT NOT NULL,
  base_date        TEXT NOT NULL,
  base_close       REAL NOT NULL,
  close            REAL NOT NULL,
  actual_change    REAL NOT NULL,       -- 小数比例
  actual_direction TEXT NOT NULL CHECK (actual_direction IN ('up','down','flat')),
  basis            TEXT NOT NULL,        -- 'prevClose' | 'naive'
  source           TEXT NOT NULL,
  ruleset_ver      TEXT NOT NULL,
  settled_at       TEXT NOT NULL,
  PRIMARY KEY (symbol, target_date)
);

-- 作业运行记录：漏跑检测的对照
CREATE TABLE IF NOT EXISTS market_forecast_runs (
  run_date   TEXT PRIMARY KEY,           -- 作业应跑的交易日
  status     TEXT NOT NULL,             -- 'ok' | 'partial' | 'failed' | 'holiday'
  detail     TEXT,
  fired_at   TEXT
);
```

### 4.2 不可变性的实现与账本的取舍

tianji 用 append-only JSONL + hash 链账本防篡改；本项目改用更轻的两层：

1. **TRIGGER 拒改**：`market_predictions` 上建
   `BEFORE UPDATE OR DELETE ... BEGIN SELECT RAISE(ABORT, 'market_predictions is append-only'); END`。
   SQLite 触发器对任何写入路径生效（含手改库），比应用层约束硬。
2. **content_hash**：全字段 canonical JSON 的 sha256。读取接口可复核，
   事后审计能发现绕过触发器的手改（改了字段对不上 hash）。

不迁 hash 链的理由：链式账本防的是「删除中间记录后重排」，本项目场景里
预测表 PK 含 origin 与全部版本维度、触发器禁删禁改，威胁模型已覆盖；
账本链的运维成本（校验失败要停机等人）对单人项目不划算。**这是一次有意识的降级，
不是遗漏。**

### 4.3 准确率的派生方式

准确率**不落库**，查询时由
`market_predictions(origin='live') ⋈ market_forecast_outcomes` 现算。
口径变更（例如将来调 flat 阈值）必须 bump `ruleset_ver`，届时新旧样本按
`ruleset_ver` 分桶展示，不混算。

## 5. 引擎移植的正确性保障

### 5.1 PRNG 逐位一致

`Math.imul`（32 位截断乘法）→ Python `(a * b) & 0xFFFFFFFF`（大整数截断，语义相同）；
`>>> 0` → `& 0xFFFFFFFF`。移植后同一 (material, domain) 必须产出与 tianji
**完全相同的字节流**，这是跨实现对账的前提。

### 5.2 黄金向量测试（对齐 tianji）

在 tianji 仓库跑一个小脚本（或利用其现成测试夹具），dump 一组
`(symbol, targetDate, seedSalt, asOf) → {爻值, 本卦, 变卦, direction, strength, id}`
的 JSON fixture，放进 `apps/api/tests/fixtures/forecast_golden.json`。
Python 引擎必须逐字段复现。fixture 特意覆盖：

- 普通交易日；
- **节气交当日**（月柱临界，验证 `getMonthInGanZhiExact` 两实现同口径）；
- 卦中无妻财爻的分支（score 分支覆盖）；
- 含动爻 + 回头生/回头克的分支。

### 5.3 版本纪律（沿 tianji module-design §7.3）

- `ENGINE_VERSION`：起卦/装卦算法或 PRNG 任何改动 → 必 bump；
- `RULESET_VERSION`：`RuleConfig` 权重或阈值任何改动 → 必 bump；
- bump 后新记录自然走新版本维度（id 含版本），历史记录不重算不覆盖；
- 准确率页按 `ruleset_ver` 分桶，杜绝跨版本混算。

## 6. 护栏清单（逐条搬，含落点）

| # | tianji 护栏 | 本项目落点 |
|---|---|---|
| 1 | 未来函数拦截：`asOf < targetDate` 否则抛错 | `engine.divine()`，错误信息照搬原话（解释为什么） |
| 2 | live 记录的记录时刻必须早于目标日 | 同上；**服务端无 `--now` 概念**，作业用服务器时钟，天然满足 |
| 3 | 时钟护栏（`--now` 与系统钟差>2h 拒跑） | 不需要等价物：不暴露自定义 now；回补历史走 origin='replay' 专用入口，replay **不进准确率** |
| 4 | 同种子必同卦（不允许摇到满意为止） | 种子只含 `symbol\|targetDate\|salt`，代码红线 + 单测断言种子材料不含任何行情字段 |
| 5 | 预测写入即冻结 | §4.2 触发器 + content_hash |
| 6 | 漏跑可见、不伪造补跑 | `market_forecast_runs` 对照 `trade_calendar`，页面列出缺口；**缺口就是缺口**，不回填 live |
| 7 | 结算拿不到 ≠ 未命中 | 无行情的目标日留在「待结算」，次日幂等补结算；不计入分母 |
| 8 | 平盘对称口径 | `hitOf`：预测或实际任一为 flat → 该样本不计入二元统计 |
| 9 | 准确率必须整套给 | API 返回结构强制含 n/hits/rate/CI/p 值/基线/样本充足性（<120 标「不足」）/note 全文，**不存在只给百分比的接口形态** |
| 10 | 0 命中 ≠ 作业成功 | 作业退出语义：交易日跑了但 0 产出 → run 记 failed（对应 tianji 退出码 3） |
| 11 | 免责声明 | 页面页脚 + API 字段 `disclaimer: '仅供娱乐，不构成投资建议'` |

## 7. 调度与作业

- **job 注册**：`services/scheduler.py` 的 `_build_scheduler()` 增加一项，
  `id="market-forecast"`，`CronTrigger(hour=15, minute=45, day_of_week="mon-fri")`，
  `coalesce=True, max_instances=1`，与现有 job_defaults 一致；
- **时间选择**：15:30 已有 returns-backfill（拉个股日线）；指数日线 EM 收盘后
  即可得，15:45 留 15 分钟余量。若仍取不到当日 bar，结算留待结算、次日补——
  幂等设计使「早了」不致命；
- **串行**：新增独立 `_forecast_lock`（15:45 与日盘扫描 09:45–14:20 天然错开，
  不与 `_scheduled_scan_lock` 互锁）；
- **配置**（`config.py` 新增，均有默认值）：
  - `MARKET_FORECAST_ENABLED=true` —— 关掉即整体停用（job 不注册）；
  - `MARKET_FORECAST_SYMBOL="000001.SH"`；
  - `MARKET_FORECAST_SEED_SALT="yz-standard-v1"` —— 默认与 tianji 同值可对账；
    改它 = 换档案，必须同步改 profile_hash 常量并在文档记录；
- **单 worker 约束兼容**：作业纯同步（akshare 指数日线一次调用），无子进程、
  无内存队列，与「后端必须单 worker」的既有约束无冲突；
- **指数日线来源**：`providers/akshare_client.py` 新增 `fetch_index_daily()`：
  主源 `ak.index_zh_a_hist(symbol="000001", period="daily")`，失败退
  新浪（与项目内 `fetch_daily` 的 EM→新浪惯例一致）。指数无除权问题，
  `basis='naive'`（前根收盘）对指数恒等于交易所口径——如实落库，不加戏。

## 8. API 设计（`main.py`，全部只读 + 登录）

| 方法与路径 | 返回 |
|---|---|
| `GET /api/market/forecast/latest` | 当前生效预测（target_date ≥ today 最近一条）+ 最近一条已结算的（含 outcome 与命中） |
| `GET /api/market/forecast/history?limit=30` | 列表：预测方向/强度/卦名 vs 实际方向/涨跌幅/命中/待结算标记 |
| `GET /api/market/forecast/accuracy?window_days=0` | §6-9 的完整报告结构（note 全文含基线警示措辞） |
| `GET /api/market/forecast/runs?limit=30` | 运行记录 + **缺口列表**（有交易日无预测） |

不提供任何「手动起卦」写接口——预测只能由作业产出（这是护栏 2/4 的一部分：
界面上没有「再摇一次」按钮）。回补历史（origin=replay）二期再议，一期不做。

## 9. 前端（`apps/web`）

新增 `app/market/page.tsx`（导航入口「大盘预判」）：

- **当前预测卡**：方向（涨/跌大字，红涨绿跌）、强度、本卦→变卦、用神、
  reasoning 依据链（完整展示，不折叠成结论）、as_of / target_date；
- **近况表**：近 30 日预测 vs 实际，命中着色，待结算置灰；
- **准确率卡**：rate + CI、基线（随机 50% / 永远猜涨）、p 值、样本充足性、
  note 全文——**没有单独的百分比大字**；
- **缺口列表**：漏跑的交易日明说；
- 页脚免责声明。

`lib/api.ts` 若新增 label 查表，与后端字段保持同步（项目已知约定）。

## 10. 测试计划

| 文件 | 覆盖 |
|---|---|
| `tests/test_forecast_prng.py` | 黄金向量：PRNG 字节流、同种子同卦 |
| `tests/test_forecast_engine.py` | 时间边界抛错、live 校验、id 稳定性、origin 维度 |
| `tests/test_forecast_dress_rules.py` | 装卦黄金向量、无妻财/回头生/动爻分支、reasoning 落点（总结论在最后） |
| `tests/test_forecast_stats.py` | wilson/p 值/threshold 与 tianji 数值对齐（n=60/120/250 三档） |
| `tests/test_forecast_service.py` | 结算幂等、补结算、待结算不计分母、触发器拒改、0 产出记 failed、节气交当日月柱 |

## 11. 落地顺序

1. **P1 引擎**：`forecast/` 包核心（prng→seed→cast→dress→rules→engine→calendar_ctx）
   + 黄金向量对齐（不含 DB）；
2. **P2 数据与作业**：DDL + `fetch_index_daily` + service + scheduler 注册 + 护栏测试；
3. **P3 接口与页面**：4 个 API + `/market` 页；
4. **P4 可选**：replay 回补历史、偏置匹配置换检验、扩展多标的。

## 12. 风险与开放问题

- **akshare 指数日线的收盘可得时刻**：15:45 EM 未出当日 bar 的概率待实测；
  幂等补结算已化解，最坏情况是准确率晚一天更新；
- **lunar-python 与 lunar-typescript 的口径漂移**：同作者但分仓维护，
  节气边界的黄金向量必须覆盖（交当日前后一小时各取一例）；
- **公开仓库**：本功能无密钥（seedSalt 不是凭证），无新增敏感信息；
- **换挡成本**：`rules.py` 权重调优诱惑很大——版本纪律（§5.3）是唯一防线，
  页面上准确率分桶展示让「调参后的成绩单」与旧样本天然隔离。

---

# 附录 A · 实施记录（P1–P4 已完成）

本节记录**落地与设计不一致的地方，以及落地过程中被测试逼出来的真缺陷**。
设计文档本身不改（它记录的是当时的判断），偏差在这里如实列出。

## A.1 与设计不一致的四项

| 项 | 设计 | 实际 | 原因 |
|---|---|---|---|
| 平盘阈值 | §6 未写单位 | `flat_threshold_pct = 0.05`，**单位是百分点**（= 比例 0.0005） | 传 0.0005 当百分点用会把几乎所有真实平盘算成涨跌，样本量凭空膨胀 |
| 真太阳时 | 未提 | **不迁** | 实测：`hour_branch` 在 2/8 个黄金向量上与 tianji 不同，而其余全字段逐字节一致 → 它不参与判决。不迁一条不参与计算的修正，否则会让人以为它算了什么 |
| accuracy 接口 | `window_days=0` | 同上，另加 `permutation` / `pending_count` 两个字段 | 置换检验样本 <20 时为 `null`；待结算数必须可见 |
| 接口数 | 4 个 | 4 个 + 1 个 admin 只读 `verify/{id}` | 复核 `content_hash`（触发器挡不住绕过 SQLite 直接改文件） |

新增常量 `SETTLE_BASE_LOOKBACK_DAYS = 20`（见 A.2-③）。

## A.2 落地过程中发现的三个真缺陷

三个都**不抛异常**，只让结果「看起来正常」或「少一点」。

### ① 出口判据恒假 → 中心环节静默坏掉仍记成功

原写法 `if created is None and not notes: status = "failed"`。
`created is None` 的两条来路都会先 append 一条 note，`not notes` **恒假** →
`failed` 分支永远不可达 → 起卦坏掉时作业照样记 `ok`，
而「漏了一天样本」的历史看起来和正常历史一模一样。

**改为状态式判据**：作业跑完，检查「目标日的 live 预测是否存在」。
它与「本轮是否创建」无关（重跑不误报失败），但能揪出所有该报警的情形。
配用例 `test_daily_run_reports_failed_when_target_has_no_prediction`
与 `test_daily_run_reports_failed_on_exception_in_divine`。

### ② 「次日用官方日线修正实时口径」这条机制执行不到

原写法把修正判断放在「待结算队列」的循环里。而 `list_pending_settlement`
的定义就是「没有 outcome 的记录」——实时口径结算**恰恰写入了 outcome**，
于是该记录立刻离开队列，第二次作业根本看不到它。
结果：文件头把机制写得很清楚，代码也"看起来实现了"，但那条路径永远走不到。

**改为独立复核通道** `_reconcile_realtime()`，并区分两件事：
`corrections`（数值真的变了）与 `confirmed`（数值一致，只是把 source 换成官方口径）。
混为一谈会让「修正了多少条」失去含义。
配用例 `test_official_bar_corrects_realtime_settlement`
（旧实现在此会得到 `corrections == []`）。

### ③ 最早的待结算日永远结不掉（窗口起点 off-by-one）

判定「某日涨跌」需要它**前一根** bar 作基准，而行情查询窗口的起点被设成
「最早的待结算日」→ 基准 bar 落在窗口外 → `change_at(bars, 0)` 返回 `None`
→ **最早那一条永远卡在待结算队列里**。没有任何报错，只表现为「样本还少」。

**修**：窗口向前回看 `SETTLE_BASE_LOOKBACK_DAYS = 20` 个自然日
（覆盖春节/国庆连休约 9~10 天，留一倍余量；本地 SQLite 查询，放宽成本可忽略）。
配用例 `test_earliest_pending_target_can_actually_settle`
与 `test_multiple_pending_days_all_settle_in_one_run`。

## A.3 与 tianji 的唯一一处声明分叉

`stats.build_accuracy_report` 的「显著」档文案，tianji 结尾是
「（见回测评估报告）」——它把置换检验挂在另一个独立模块里。
本项目没有那个模块（置换结果就在同一份报告的 `permutation` 字段里），
照抄会让界面指向不存在的页面。

现改为「（见 permutation 字段）」，并在 `test_forecast_stats.py` 里用
`DECLARED_NOTE_SUBSTITUTION` **显式写出这一处替换**、断言「恰好只有这一处」：
其它任何字面漂移仍然会让用例失败。

## A.4 黄金向量对账结果

| fixture | 来源 | 结果 |
|---|---|---|
| `forecast_golden.json` | tianji 引擎 dump（`dump.mjs`） | 8/8 用例全字段一致（含整条中文依据链） |
| `forecast_prng_golden.json` | tianji `Prng` + `derivePrng` dump | 逐位一致（含交叉调用序列、最小 4 字节种子、全零种子替换） |
| `forecast_stats_golden.json` | tianji `stats` + `permutation` dump | 数值一致（容差 1e-12），除 A.3 那一处文案 |

`profile_hash = 0da2843edf6479f1` 两边相同。

**对账过程本身的数据**（值得记下来）：第一轮 13 个红里，
10 个是对账脚本自己的口径错、2 个是我把笔记里的数字记错了
（`threshold(250)` 是 **55.6%** 不是 55.2%）、**1 个是真差异**。
所以复核对账结果的第一步永远是「先分清谁错」，不是「先改代码」。

## A.5 一个实测出来的设计事实：财不上卦占 37.5%

八条黄金向量里有 **3 条**是「财不上卦」（雷山小过 / 天泽履 / 风泽中孚）：
规则命中 `no_wealth_in_hexagram = -0.15` → 得分固定 0.350 → **一律判跌**。

即：**模型的预测输出本身不是 50/50 的**，约三分之一的日子会因这条分支被固定判成跌。
这正是报告里那些基线存在的理由——若某天「永远猜跌」基线特别高，
多半是这条分支在起作用，而不是卦象有信息量。
`test_no_wealth_branch_is_a_documented_systematic_down_tilt` 把这条钉住。

## A.6 P4 的落地形态

`backfill_replay()` 已实现，但**刻意不做成接口**：任何"手动补一条"的入口
都等于允许事后挑一个能摇出好卦的日子。落地为离线脚本
`scripts/forecast_backfill.py`，且**不提供 `--as-of` 覆盖参数**——
起卦日一律取「目标日的上一个交易日」，由交易日历算出，不由人挑。

## A.7 验收状态

- 后端测试：**351 passed**（新增 6 个预判测试文件，共 230 个用例）
- 前端：`tsc --noEmit` 0 错；`next build` 通过（`/market` 8.04 kB）
- 调度：`market-forecast` @ 15:45 mon-fri，独立 `_forecast_lock`，与 15:30 回填错开
- 真实行情链路：`fetch_index_daily` 实测可用（东财被本机代理挡住 → 新浪顶上，落 `source='sina'`）；15:45 通常取不到当日官方 bar → 走实时口径先结，次日修正（A.2-②）
