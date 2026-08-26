from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    anomaly_warn_pct: float = 180.0
    anomaly_block_pct: float = 195.0
    demo_mode: bool = False  # True 时用内置样例，不请求行情
    # Windows 常见缺 CA：默认关闭校验以便拉东财；正式环境可设 SSL_VERIFY=true
    ssl_verify: bool = False

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


settings = Settings()
