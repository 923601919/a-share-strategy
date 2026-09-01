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

    # 策略/软件版本（复盘可回溯）
    strategy_version: str = "2026.09.01-sw"
    scan_use_isolated: bool = True  # 危险行情路径走子进程（全市场快照）
    sector_universe_use_isolated: bool = False  # 新浪板块走子进程易超时，默认进程内
    # 超时后线程可能空转：热点路径优先子进程硬杀
    prefer_isolated_timeout: bool = True
    scan_max_concurrent: int = 1  # 同时 running 的扫描数
    scan_snapshot_keep: int = 30  # scan_snapshots 保留条数
    spot_cache_ttl: float = 45.0
    universe_cache_ttl: float = 90.0


settings = Settings()


def auth_is_required() -> bool:
    if settings.auth_required is not None:
        return bool(settings.auth_required)
    return bool((settings.jwt_secret or "").strip())
