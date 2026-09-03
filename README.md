# 分时雷达 · A股策略选股与跟踪

进攻型分时选股 + 自选跟踪 MVP。研究工具，非投资建议。

## 结构

```
apps/api   FastAPI + akshare + 规则引擎 + SQLite
apps/web   Next.js 选股/跟踪页
data/      数据库与缓存
```

## 启动

已提供脚本（推荐）：

```powershell
# 终端 1
cd D:\project\pythonproject\a-share-strategy
.\scripts\start-api.ps1

# 终端 2
.\scripts\start-web.ps1
```

默认 **真实行情**（东财 via akshare）。仅调试用演示数据：

```powershell
$env:DEMO_MODE="true"
.\scripts\start-api.ps1
```

### 手动方式

#### 1) API

```powershell
cd D:\project\pythonproject\a-share-strategy\apps\api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DEMO_MODE="true"
uvicorn main:app --reload --port 8000
```

健康检查：http://127.0.0.1:8000/api/health

#### 2) Web

另开终端（需 Node 在 PATH，或使用 `C:\Program Files\nodejs`）：

```powershell
cd D:\project\pythonproject\a-share-strategy\apps\web
npm install
$env:NEXT_PUBLIC_API_BASE="http://127.0.0.1:8000"
npm run dev
```

打开：http://localhost:3000

## 功能

- **选股**：涨幅/成交额初筛 → 情绪温度计 + 板块生命周期 + 龙头分层 → 分时得分（回踩再攻、逐波量能、炸板质量）→ 30 日异动/监管日历过滤
- **跟踪**：自选列表刷新行情，展示异动进度、MA5、竞价卖点提示
- **验证**：`/stats` 分数有效性验证——入池分数分桶 × T+N 胜率/收益，检验打分是否真的有效（纯本地库计算）
- 个股代码可点到东方财富分时页人工确认

## 环境变量（API）

见仓库根目录 [`.env.example`](.env.example)。常用项：

| 变量 | 含义 | 默认 |
|------|------|------|
| `DEMO_MODE` | true 时用演示数据 | false |
| `SSL_VERIFY` | 校验证书；本地 Windows 脚本未设置时为 false | true（config） |
| `API_KEY` | 非空则需 `X-API-Key` | 空 |
| `JWT_SECRET` | 非空则开启多人登录（JWT） | 空 |
| `AUTH_REQUIRED` | 显式开关；空则随 `JWT_SECRET` | 自动 |
| `DOCS_ENABLED` | 是否暴露 `/docs` | true |
| `CORS_ORIGINS` | 允许的前端源 | localhost:3000 |
| `SCAN_MAX_CONCURRENT` | 同时 running 扫描数 | 1 |
| `MIN_AMOUNT_YI` | 成交额下限（亿） | 1 |
| `ANOMALY_WARN_PCT` | 异动警告阈值 | 180 |
| `ANOMALY_BLOCK_PCT` | 异动剔除阈值 | 195 |
| `EXCLUDE_STAR_MARKET` | 剔除科创板（688/689） | true |
| `EXCLUDE_BSE` | 剔除北交所/老三板（4/8/92 开头） | true |
| `DAILY_CACHE_ENABLED` | 日线按日缓存（盘前/收盘后缓存到下一交易日） | true |
| `DAILY_CACHE_INTRADAY_TTL` | 盘中日线缓存秒数 | 1800 |
| `CHASE_PENALTY_ENABLED` | 追高惩罚（避免拉升中的票因量能/斜率虚高） | true |
| `CHASE_POS_HIGH` | 日内位置 ≥ 此值判为逼近日内高位 | 0.90 |
| `CHASE_POS_PENALTY` | 逼近日内高位的降权分 | 15 |
| `CHASE_DEV_HIGH` | 正乖离均价 ≥ 此百分比(%)判为乖离过大 | 2.5 |
| `CHASE_DEV_PENALTY` | 乖离过大的降权分 | 12 |
| `MINUTE_MIN_ROWS` | 分时最少行数（正常时段） | 15 |
| `MINUTE_MIN_ROWS_EARLY` | 早盘放宽后的最少行数 | 8 |
| `MINUTE_EARLY_WINDOW_MIN` | 开盘后多少分钟内走 early 阈值 | 30 |

策略打分参数（回踩带、量能档位、斜率阈值、各项加减分等）已全部集中到
`apps/api/config.py` 的「策略打分参数」区，可用环境变量覆盖；规则函数通过
`rules/params.py` 的 `StrategyParams` 取值。**调整参数请同步 `STRATEGY_VERSION`**，
复盘与验证页按版本回溯才有意义。

前端见 [`apps/web/.env.example`](apps/web/.env.example)（`NEXT_PUBLIC_API_BASE` / `NEXT_PUBLIC_API_KEY`）。

### 多人（给朋友用）

1. `.env` 设置 `JWT_SECRET`（随机长串）与生产 `CORS_ORIGINS`
2. 创建管理员：`python scripts/create_admin.py --username alice --password '******'`
3. 生成邀请码：同脚本加 `--invite`，或登录后点导航「邀请码」
4. 朋友打开 `/login` →「用邀请码注册」；每人自选 / 模拟盘互不共享
5. 生产建议 `DOCS_ENABLED=false`，必要时再加反向代理 HTTPS

同步 OpenAPI 到前端：`python scripts/export-openapi.py`（需 API 依赖）。

（也可改 `config.py`）

## 说明

- 免费行情源有延迟与限流，扫描请控制频率
- 「进攻型」盘感无法完全量化，结果需人工看分时图确认
- API 默认绑定 `127.0.0.1`；给朋友用请设 `JWT_SECRET`（并建议反代 + HTTPS）