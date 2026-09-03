from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(Path(__file__).resolve().parent / ".env"), str(ROOT / ".env")),
        extra="ignore",
    )

    db_path: Path = DATA_DIR / "app.db"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # 扫描默认参数
    min_amount_yi: float = 1.0  # 成交额下限（亿元）
    min_pct: float = 2.0  # 涨幅下限（%）
    max_pct: float = 6.0  # 当前涨幅严格小于此值
    leader_dip_min_pct: float = -3.0  # 龙头低吸涨幅下限
    leader_dip_max_pct: float = 2.0  # 龙头低吸涨幅上限（含）
    proxy_score_cap: float = 50.0  # 分时不可用时代理分上限
    minute_min_rows: int = 15  # 分时正常时段最少分钟数
    minute_min_rows_early: int = 8  # 早盘可放宽的最少分钟数（开盘 30 分钟内）
    minute_early_window_min: int = 30  # 开盘后多少分钟内走 early 阈值
    sector_min_pct: float = 0.0  # 板块涨幅低于此不入候选池
    max_candidates_spot: int = 80
    top_n_result: int = 30
    # 测试策略：配额制卫星池占比（非主线名额）
    universe_quota_satellite_pct: float = 0.25
    # 软加权：热门板块加分（仅 soft 策略对进攻型生效）
    soft_hot_board_bonus: float = 8.0
    anomaly_warn_pct: float = 180.0
    anomaly_block_pct: float = 195.0
    # 进攻型分时：今累计成交/昨全日（优先成交额）
    day_vol_block_by_1000: float = 1.0  # ≤10:00 且 ≥昨全日 → 剔除
    day_vol_block_by_1130: float = 2.0  # ≤11:30 且 ≥2×昨量 → 剔除
    day_vol_warn_by_1130: float = 1.2  # ≤11:30 且 ≥1.2× → 降权
    demo_mode: bool = False  # True 时用内置样例，不请求行情
    # 默认校验证书；Windows 缺 CA 时设 SSL_VERIFY=false
    ssl_verify: bool = True
    # 非空则要求请求头 X-API-Key（本地研究可留空；多人部署建议配合 JWT）
    api_key: str = ""
    # 多人：设 JWT_SECRET 后默认开启登录；本地可留空关闭鉴权
    jwt_secret: str = ""
    jwt_expire_hours: int = 168  # 7 天
    # None=自动（有 jwt_secret 则开）；显式 true/false 可覆盖
    auth_required: bool | None = None
    # 生产建议 false，避免公开 OpenAPI
    docs_enabled: bool = True

    # 模拟盘（默认 / 按自选来源）
    sim_initial_capital: float = 100_000.0
    sim_max_positions: int = 5
    sim_position_pct: float = 0.2  # 单票目标仓位占权益比例
    sim_take_profit_pct: float = 7.0  # manual 默认止盈%
    sim_stop_loss_pct: float = 3.0  # manual 默认止损%
    sim_take_profit_pct_fenshi: float = 8.0  # 进攻型分时：让利润奔跑
    sim_stop_loss_pct_fenshi: float = 3.0
    sim_take_profit_pct_longtou: float = 5.0  # 龙头低吸：有赚先走
    sim_stop_loss_pct_longtou: float = 2.5
    sim_commission_rate: float = 0.0003  # 佣金万三
    sim_min_commission: float = 5.0

    # 复盘：外盘偏弱判定（美股主要指数均涨跌幅%）
    global_weak_avg_pct: float = -0.5
    global_weak_index_pct: float = -0.8  # 单指数跌超此值计为偏弱

    # ===== 策略打分参数（魔数集中管理；调整参数请同步 bump strategy_version） =====
    # 通用 / 进攻型分时
    fenshi_lookback: int = 30  # 形态检测回看K线数
    fenshi_vwap_tolerance: float = 0.002  # 站稳均价容差（现价 >= 均价*(1-容差)）
    fenshi_pullback_band: float = 0.008  # 回踩判定带 ±0.8%
    fenshi_tail_bars: int = 6  # 再攻末段K线数
    fenshi_tail_above_min: int = 4  # 末段需站上均价的最少根数
    fenshi_near_high_factor: float = 0.98  # 逼近日内高位判定
    fenshi_score_cap: float = 100.0
    attack_window_bonus: float = 5.0  # 核心买点窗口加分
    # 追高惩罚：买入位置越接近日内高点、离均价乖离越大 -> 越是追高而非回踩买点
    chase_penalty_enabled: bool = True
    chase_pos_high: float = 0.90  # 日内位置 >= 此值判为逼近日内高位（0=最低 1=最高）
    chase_pos_penalty: float = 15.0
    chase_dev_high: float = 2.5  # 现价高于均价超过此百分比(%)判为乖离过大
    chase_dev_penalty: float = 12.0
    # 形态得分
    score_strong_push: float = 38.0
    score_pullback_reattack: float = 40.0
    score_pullback_only: float = 18.0
    score_reattack_only: float = 22.0
    score_above_vwap: float = 15.0
    # 量能
    vol_hot: float = 1.8  # 放量倍数（高档）
    vol_hot_score: float = 25.0
    vol_mild: float = 1.3  # 量能略增（低档）
    vol_mild_score: float = 12.0
    vol_recent_window: int = 5  # 近段量能窗口
    vol_prior_window: int = 15  # 前段量能窗口（含近段）
    # 斜率
    slope_window: int = 8  # 斜率计算窗口（根）
    slope_hot: float = 1.0
    slope_hot_score: float = 20.0
    slope_mild: float = 0.4
    slope_mild_score: float = 10.0
    # 强势推升判定阈值
    strong_push_min_above_ratio: float = 0.75
    strong_push_min_slope: float = 0.8
    strong_push_min_vol: float = 1.3
    # 日量健康 / 假推升降权
    day_vol_healthy_bonus: float = 5.0
    day_vol_warn_penalty: float = 15.0
    false_push_penalty: float = 25.0
    no_pattern_penalty: float = 15.0  # 进攻型未确认形态降权
    # 龙头低吸
    ld_pct_low: float = -2.0
    ld_pct_high: float = 0.5
    ld_pct_high_score: float = 28.0
    ld_pct_mild: float = 1.5
    ld_pct_mild_score: float = 18.0
    ld_ma5_near: float = 1.5  # 贴近MA5距离%
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

    # ===== 大盘环境闸门 =====
    # 指数暴跌日普跌，逆势拉升分时次日易低开补跌，需整体降级/观望
    market_env_enabled: bool = True
    market_env_warn_pct: float = -1.5  # 上证当日跌幅 <= 此值：结果全体分数折减 + 风险横幅
    market_env_block_pct: float = -2.5  # 上证当日跌幅 <= 此值：进攻型观望（不出结果）
    market_env_score_factor: float = 0.8  # 警告档：进攻型分数乘此系数
    market_env_ref_index: str = "sh000001"  # 主参考指数

    # ===== 风险过滤 =====
    exclude_star_market: bool = True  # 科创板 688/689（20%涨跌幅、权限门槛）
    exclude_bse: bool = True  # 北交所/老三板（4/8/92 开头，30%涨跌幅）

    # ===== 行情缓存 =====
    daily_cache_enabled: bool = True
    daily_cache_intraday_ttl: float = 1800.0  # 盘中日线缓存秒数（最后一根K会变）

    # 策略/软件版本（复盘可回溯）
    strategy_version: str = "2026.09.02-macro"
    scan_use_isolated: bool = True  # 危险行情路径走子进程（全市场快照）
    sector_universe_use_isolated: bool = False  # 新浪板块走子进程易超时，默认进程内
    # 超时后线程可能空转：热点路径优先子进程硬杀
    prefer_isolated_timeout: bool = True
    scan_max_concurrent: int = 1  # 同时 running 的扫描数
    scan_snapshot_keep: int = 30  # scan_snapshots 保留条数
    spot_cache_ttl: float = 45.0
    universe_cache_ttl: float = 90.0
    enrich_timeout_seconds: float = 120.0  # enrich 阶段整体预算，超时未完成的候选直接跳过
    # 全市场快照隔离子进程超时（实测抓取约 15s，需留足 pickle/传输余量）
    spot_isolated_timeout: float = 60.0


settings = Settings()


def auth_is_required() -> bool:
    if settings.auth_required is not None:
        return bool(settings.auth_required)
    return bool((settings.jwt_secret or "").strip())
