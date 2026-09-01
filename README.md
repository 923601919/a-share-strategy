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

- **选股**：涨幅/成交额初筛 → 热门概念加权 → 分时得分（站上均价、斜率、量能）→ 30 日异动红线过滤
- **跟踪**：自选列表刷新行情，展示异动进度、MA5、竞价卖点提示
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