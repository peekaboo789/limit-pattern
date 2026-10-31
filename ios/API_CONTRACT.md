# iOS 客户端依赖的接口

App 只通过 Docker 版的 `/api/*` 工作；回测、聚类、涨停池、设置用 WKWebView 打开网页版对应路由，共用同一个 cookie。

## 登录（0.5.1 设计，兼容 0.5.0）

| 方法 | 路径 | App 的用法 |
| --- | --- | --- |
| GET | `/api/health` | 不需登录。读 `version`、`data_date`、`stored_days`、`data_source`、`active_source`。`version` < 0.5.0 时提示升级 |
| GET | `/api/auth/status` | `{required, logged_in, headless}`；0.5.1 预期另有 `username_required` / `has_users` / `username`（任一存在就显示账号框为必填） |
| POST | `/api/auth/login` | 正文 `{"username", "password", "remember": true}`。0.5.0 只读 `password`，多余字段被忽略，所以同一请求两版都能用。成功后服务器下发 HttpOnly cookie `lp_session` |
| POST | `/api/auth/logout` | 退出 |

约定（0.5.1 需要满足）：
- 会话 cookie 名仍是 `lp_session`，带 `Max-Age`（长期有效）；改密码或「退出所有设备」后旧 cookie 失效，任意 `/api/*` 返回 **401**，App 收到 401 就回到登录页。
- 登录失败返回 401/403/429，`detail` 为中文原因（例如锁定 5 分钟），App 原样显示。
- `/api/health` 继续免登录。

App 把 cookie 存在系统 `HTTPCookieStorage`，并把值备份到钥匙串；系统清掉 cookie 时用备份补回，服务器仍按签名判断是否有效。

## 业务接口

总览：`GET /api/sync/status`、`POST /api/sync`、`GET /api/monitor`、`POST /api/snapshot/capture`（409 时提供 `{"fallback": true}`）、`GET /api/snapshot/capture/status`、`POST /api/snapshot/capture/cancel`、`GET /api/snapshot/manual`、`GET /api/ai-input?snapshot=manual&format=txt|md`、`POST /api/ai-ranking/generate {"snapshot":"manual"}`。

榜单：`GET /api/ai-ranking?date=`、`GET /api/ai-ranking/dates`、`POST /api/ai-ranking/generate`（`snapshot`: manual / morning）、`GET /api/ai-ranking/progress`、`POST /api/ai-ranking/import {text, date?, model?}`、`GET /api/export?type=ai_ranking&date=`、`GET /api/ai-input?format=`。

监控：`GET /api/monitor`（5 秒轮询，休市 60 秒）、`GET /api/monitor/events?since=`（本地通知）。

自选：`GET /api/watchlist/monitor`、`GET /api/watchlist`、`GET /api/watchlist/search?q=&limit=`、`POST /api/watchlist`、`POST /api/watchlist/delete`、`POST /api/watchlist/note`、`POST /api/watchlist/reorder`、`POST /api/watchlist/import/preview`、`GET /api/watchlist/export?format=txt|csv`。

更多：`GET /api/scan?top=100`、`GET /api/similarity?code=&top=20`、`GET /api/export?type=scan|next_day_pool`、`GET /api/next-day-pool?top=30`、`GET /api/summaries`、`GET /api/summaries/{date}`、`POST /api/summaries/{date}/generate`、`GET /api/daily-summary/export?start=&end=&format=`。

所有导出读 `Content-Disposition`（支持 `filename*=UTF-8''`）当文件名，存到临时目录后用系统分享面板（可「存储到文件」）。
