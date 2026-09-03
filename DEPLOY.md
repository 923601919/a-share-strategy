# 部署说明（Lighthouse · Docker）

项目「分时雷达 · A股策略选股与跟踪」的容器化部署记录与运维手册。

## 架构

- **Nginx 反代统一 80 端口**：`nginx:alpine` 容器是公网唯一入口，`/api/*` 透传给 FastAPI，其余给 Next.js 前端 —— 前端 + API **同源**，彻底消除 CORS。
- 前端构建时 `NEXT_PUBLIC_API_BASE=""`（空串 = 同源相对路径，任意 IP/域名通用）。
- api / web 容器端口只绑 `127.0.0.1`（仅供宿主机调试），不对公网暴露；**JWT 登录保护保持不变**。

```
浏览器 ──HTTP 80──▶ nginx ──/api/*──▶ api:8000 (FastAPI + akshare)
                         └──其余───▶ web:3000 (Next.js standalone)
```

## 服务器信息

| 项      | 值                                                        |
| ------ | -------------------------------------------------------- |
| 实例     | `lhins-6d101ib2`（上海 ap-shanghai，2C2G，TencentOS Server 4） |
| 公网 IP  | `124.222.194.44`                                         |
| 代码目录   | `/opt/a-share-strategy`                                  |
| 数据目录   | `/opt/a-share-strategy/data`（SQLite，Docker 卷持久化）         |
| Docker | CE 29.7.2 + Compose v5.4（TencentOS 原生 EPOL 源安装）          |

## 访问方式（公网直连）

浏览器直接打开 <http://124.222.194.44> 。首次使用：`bootstrap_available=true`，在登录页创建首个管理员账号。

- 防火墙：Lighthouse 防火墙放行 TCP 80（0.0.0.0/0）。
- 生产环境 `DOCS_ENABLED=false`（`/docs` 不受 JWT 保护，已关闭）。
- HTTPS：后续升级需域名 + 证书（届时 Nginx 加 443 server 块即可，应用层零改动）。

## 日常更新（发布新版本）

```bash
# 本地（Windows）提交并推送
git add -A && git commit -m "..." && GIT_TERMINAL_PROMPT=0 git push origin main

# 服务器
cd /opt/a-share-strategy
git pull                                    # origin 已指向 ghfast.top 镜像
docker compose up -d --build                # 重建并重启（web 若改动较少会自动复用缓存）
```

> 改了 `apps/web/lib/api.ts` 等 `NEXT_PUBLIC_*` 相关代码必须重建 web 镜像（构建期注入，非运行时变量）。

## 关键配置

- 环境变量：`/opt/a-share-strategy/.env`（`JWT_SECRET`、`DOCS_ENABLED=false`、`SSL_VERIFY=true`、`DEMO_MODE=false`）。
- 反代配置：`deploy/nginx.conf`（改动后 `docker compose restart nginx`）。
- 构建镜像加速（国内必需）：
  - PyPI：`docker-compose.yml` 的 `PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple/`
  - Docker Hub：`/etc/docker/daemon.json` 的 `registry-mirrors`（daocloud + 腾讯）
  - GitHub：clone/pull 走 `https://ghfast.top/https://github.com/...`

## 注意

- **版本差异**：服务器上装的是 `pandas 3.0.5 / numpy 2.4.6 / akshare 1.18.94 / fastapi 0.141.1`（requirements 用 `>=` 未 pin）。全市场快照实测正常（5554 只/15s）。若后续扫描出现 pandas 3.x 兼容问题，需在 `requirements.txt` 里 pin 到本地一致的版本。
- 后端单 worker（内存任务队列 + spawn 隔离子进程不支持多进程共享），勿改多 worker。
- 容器 `restart: unless-stopped`，重启服务器会自动拉起；日志已限 10MB×3。
- 公网部署后 `JWT_SECRET` 不可泄露；`/docs` 已关闭（`DOCS_ENABLED=false`）。

## 定时扫描（软加权 / 进攻型分时自动加自选）

后端内置进程内调度器（`services/scheduler.py`，APScheduler BackgroundScheduler），**每个交易日 10:40 与 14:20** 各触发一轮扫描，并把全部命中标的自动加入对应策略账号的自选列表：

| 策略账号 | 扫描参数 | 自选来源标记 |
| -------- | -------- | ------------ |
| `soft` | `mode=fenshi` + `universe_policy=soft`（软加权） | `source=fenshi` |
| `fenshi` | `mode=fenshi` + `universe_policy=hot_only`（进攻型分时） | `source=fenshi` |

- 两个账号**完全隔离**（`watchlist` 主键 `(user_id, code)`），账号首次触发时自动创建，无需手动建。
- 调度开关与参数：`.env` 的 `SCHEDULER_ENABLED`（默认 true）、`SCHEDULER_TIMEZONE`（默认 `Asia/Shanghai`）。
- 交易日判断走 `services/trade_calendar.py`（akshare 交易日历，失败回退工作日近似）；非交易日自动跳过。
- 观察：`/api/health` 返回的 `scheduler` 字段含 4 个 job 及下次触发时间。
- 部署注意：`requirements.txt` 已加 `apscheduler`，重新 `docker compose up -d --build` 时会装入。
