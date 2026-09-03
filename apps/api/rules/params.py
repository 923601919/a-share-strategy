"""策略参数对象：把打分魔数集中到 config，规则函数通过 StrategyParams 取值。

- 默认值与历史硬编码行为完全一致。
- 所有字段可通过环境变量（.env）覆盖，因为 config.Settings 声明了同名字段。
- 调整参数请同步 bump config.strategy_version，便于复盘按版本回溯。
"""
from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class StrategyParams:
    """进攻型分时 / 龙头低吸打分参数（与 config.Settings 同名字段联动）。"""

    # 通用 / 进攻型分时
    fenshi_lookback: int = 30
    fenshi_vwap_tolerance: float = 0.002
    fenshi_pullback_band: float = 0.008
    fenshi_tail_bars: int = 6
    fenshi_tail_above_min: int = 4
    fenshi_near_high_factor: float = 0.98
    fenshi_score_cap: float = 100.0
    attack_window_bonus: float = 5.0
    # 追高惩罚（买入位置 / 乖离）
    chase_penalty_enabled: bool = True
    chase_pos_high: float = 0.90
    chase_pos_penalty: float = 15.0
    chase_dev_high: float = 2.5
    chase_dev_penalty: float = 12.0
    # 形态得分
    score_strong_push: float = 38.0
    score_pullback_reattack: float = 40.0
    score_pullback_only: float = 18.0
    score_reattack_only: float = 22.0
    score_above_vwap: float = 15.0
    # 量能
    vol_hot: float = 1.8
    vol_hot_score: float = 25.0
    vol_mild: float = 1.3
    vol_mild_score: float = 12.0
    vol_recent_window: int = 5
    vol_prior_window: int = 15
    # 斜率
    slope_window: int = 8
    slope_hot: float = 1.0
    slope_hot_score: float = 20.0
    slope_mild: float = 0.4
    slope_mild_score: float = 10.0
    # 强势推升阈值
    strong_push_min_above_ratio: float = 0.75
    strong_push_min_slope: float = 0.8
    strong_push_min_vol: float = 1.3
    # 日量健康 / 假推升降权
    day_vol_healthy_bonus: float = 5.0
    day_vol_warn_penalty: float = 15.0
    false_push_penalty: float = 25.0
    no_pattern_penalty: float = 15.0
    # 龙头低吸
    ld_pct_low: float = -2.0
    ld_pct_high: float = 0.5
    ld_pct_high_score: float = 28.0
    ld_pct_mild: float = 1.5
    ld_pct_mild_score: float = 18.0
    ld_ma5_near: float = 1.5
    ld_ma5_near_score: float = 32.0
    ld_ma5_mid: float = 3.0
    ld_ma5_mid_score: float = 18.0
    ld_open_hold_factor: float = 0.995
    ld_open_hold_score: float = 12.0
    ld_open_soft_factor: float = 0.98
    ld_open_soft_score: float = 6.0
    ld_near_vwap_factor: float = 0.992
    ld_above_vwap_score: float = 15.0
    ld_near_vwap_score: float = 8.0
    ld_tail_bars: int = 8
    ld_tail_min_bars: int = 6
    ld_tail_score: float = 10.0
    leader_dip_hot_board_bonus: float = 8.0
    # 逐波量能 / 炸板（进攻型分时增量）
    wave_vol_bonus: float = 8.0
    wave_vol_penalty: float = 10.0
    wave_vol_enabled: bool = True
    zhaban_benign_bonus: float = 8.0
    zhaban_weak_penalty: float = 12.0

    @classmethod
    def from_settings(cls) -> "StrategyParams":
        """从全局 settings 读取同名字段（settings 未声明则用默认值）。"""
        from config import settings

        data = {}
        for f in fields(cls):
            if hasattr(settings, f.name):
                data[f.name] = getattr(settings, f.name)
        return cls(**data)
