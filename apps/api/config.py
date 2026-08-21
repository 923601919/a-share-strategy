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
    min_pct: float = 2.0
    max_pct: float = 6.0  # 涨幅超过此值不考虑（避免追高）
    max_candidates_spot: int = 80
    top_n_result: int = 30
    anomaly_warn_pct: float = 180.0
    anomaly_block_pct: float = 195.0
    demo_mode: bool = False  # True 时用内置样例，不请求行情
    # Windows 常见缺 CA：默认关闭校验以便拉东财；正式环境可设 SSL_VERIFY=true
    ssl_verify: bool = False


settings = Settings()
