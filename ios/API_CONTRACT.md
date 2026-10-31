# iOS 客户端依赖的接口

App 只通过 Docker 版的 `/api/*` 工作；回测、聚类、涨停池、设置用 WKWebView 打开网页版对应路由，共用同一个 cookie。

## 登录（服务器 0.5.1，已对照 backend/app/api/wecom_routes.py）

| 方法 | 路径 | App 的用法 |
| --- | --- | --- |
| GET | `/api/health` | 免登录。读 `version`；低于 0.5.2 提示升级 |
| GET | `/api/auth/status` | `{required, logged_in, setup_needed, username, from_env, headless}`。`setup_needed` 时登录页变成「创建管理员账号」 |
| POST | `/api/auth/setup` | `{username, password}`，仅无账号时，然后接着登录 |
| POST | `/api/auth/login` | `{username, password, remember: true}` → HttpOnly cookie `lp_session`（记住我约 10 年）。5 次失败 429 锁 5 分钟，App 显示服务器的中文 `detail` |
| POST | `/api/auth/logout`、`/api/auth/logout-all` | 退出 / 退出所有设备 |

改密码或改账号名后旧 cookie 失效，任意 `/api/*` 返回 401，App 回到登录页。修改账号密码（`/api/auth/change`）在内嵌的设置网页里做。0.5.0 服务器只读 `password`，多余字段被忽略，仍能登录（App 会提示升级）。

App 把 cookie 存在系统 `HTTPCookieStorage`，并把值备份到钥匙串；系统清掉 cookie 时用备份补回，服务器仍按签名判断是否有效。

## 业务接口

总览：`GET /api/sync/status`、`POST /api/sync`、`GET /api/monitor`、`POST /api/snapshot/capture`（409 时提供 `{"fallback": true}`）、`GET /api/snapshot/capture/status`、`POST /api/snapshot/capture/cancel`、`GET /api/snapshot/manual`、`GET /api/ai-input?snapshot=manual&format=txt|md`、`POST /api/ai-ranking/generate {"snapshot":"manual"}`。

榜单：`GET /api/ai-ranking?date=`、`GET /api/ai-ranking/dates`、`POST /api/ai-ranking/generate`（`snapshot`: manual / morning）、`GET /api/ai-ranking/progress`、`POST /api/ai-ranking/import {text, date?, model?}`、`GET /api/export?type=ai_ranking&date=`、`GET /api/ai-input?format=`。

监控：`GET /api/monitor`（5 秒轮询，休市 60 秒）、`GET /api/monitor/events?since=`（本地通知）。

自选：`GET /api/watchlist/monitor`、`GET /api/watchlist`、`GET /api/watchlist/search?q=&limit=`、`POST /api/watchlist`、`POST /api/watchlist/delete`、`POST /api/watchlist/note`、`POST /api/watchlist/reorder`、`POST /api/watchlist/import/preview`、`GET /api/watchlist/export?format=txt|csv`。

更多：`GET /api/scan?top=100`、`GET /api/similarity?code=&top=20`、`GET /api/export?type=scan|next_day_pool`、`GET /api/next-day-pool?top=30`、`GET /api/summaries`、`GET /api/summaries/{date}`、`POST /api/summaries/{date}/generate`、`GET /api/daily-summary/export?start=&end=&format=`。

所有导出读 `Content-Disposition`（支持 `filename*=UTF-8''`）当文件名，存到临时目录后用系统分享面板（可「存储到文件」）。

## 数据导出（更多 → 数据导出）

`/api/ai-input?format=txt|md[&snapshot=manual][&date=]`、`/api/export?type=ai_ranking|scan|similarity|next_day_pool|pool|backtest|cluster|summary`、`/api/watchlist/export?format=txt|csv`、`/api/daily-summary/export?start&end&format=txt|md|json|csv`。

设置配置（0.5.2）：`GET /api/config/export?secrets=false|true&watchlist=false|true` → JSON 附件（App 里有「包含密钥」「包含自选股」两个开关）。导入（`POST /api/config/import/preview {file}`、`POST /api/config/import {file, watchlist?, account?}`）在内嵌设置网页里做。404 时 App 提示升级服务器。App 要求服务器 ≥ 0.5.2。
