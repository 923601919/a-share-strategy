"""大盘涨跌预判（六爻）——自 tianji 项目移植的确定性起卦 / 断卦引擎。

设计要点（详见 `docs/market-forecast-migration-design.md`）：

- **确定性**：同一个（标的, 目标日, 盐值）永远摇出同一卦。没有随机源，
  没有 `datetime.now()` 参与起卦——起卦时点只作为「as_of」被记进记录、
  并被护栏检查（必须早于目标日），不参与摇卦。
- **写入即冻结**：预测落库后由 SQLite 触发器挡住 UPDATE/DELETE，
  另存一个 `content_hash` 供事后复核。
- **准确率的诚信**：不存在"只给一个百分比"的形态。报告必然带基线、
  置信区间、精确二项 p 值、随样本量变化的显著性门槛与样本充足性标记；
  未拿到目标日行情的样本留在「待结算」，既不算命中也不算未命中。

⚠ 仅供娱乐，不构成投资建议。

模块分层（下层不依赖上层）：

    constants  prng  util                 纯计算，无外部依赖
    seed  cast  dress  rules              起卦与装卦断卦
    calendar_ctx                          干支历法（唯一用 lunar-python 的地方）
    engine                                串联上述全部 + 护栏
    change  stats                         收益口径与统计检验
    service                               落库与作业编排（唯一碰 db 的模块）
"""

from __future__ import annotations

from .profile import ENGINE_VERSION, RULESET_VERSION

__all__ = ["ENGINE_VERSION", "RULESET_VERSION"]
