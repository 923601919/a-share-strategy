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

| 变量 | 含义 | 默认 |
|------|------|------|
| `DEMO_MODE` | true 时用演示数据 | false |
| `SSL_VERIFY` | true 严格校验证书；Windows 缺 CA 时默认 false | false |
| `MIN_AMOUNT_YI` | 成交额下限（亿） | 1 |
| `ANOMALY_WARN_PCT` | 异动警告阈值 | 180 |
| `ANOMALY_BLOCK_PCT` | 异动剔除阈值 | 195 |

（也可改 `config.py`）

## 说明

- 免费行情源有延迟与限流，扫描请控制频率
- 「进攻型」盘感无法完全量化，结果需人工看分时图确认
