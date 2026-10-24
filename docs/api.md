# 接口

基址 `http://127.0.0.1:8765`。成功时直接返回 JSON。失败时 HTTP 4xx/5xx，正文是 `{"detail": "中文说明"}`。日期一律 `YYYY-MM-DD`，股票代码 6 位。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | `status`、`version`、`data_date`、`data_source`、`active_source`、`exports_dir` |
| GET | `/api/settings` | 保留天数、对比窗口、AI 是否启用、地址、模型、密钥是否已填、数据源（`auto` / `eastmoney` / `tencent` / `sina`）、实际使用的源、纳入范围 `market_scope`、`excluded_boards`、上次同步、上次清理 |
| PUT | `/api/settings` | 修改可写项。`ai_api_key` 只写入、不回显。对比窗口传了非 3 会 422 |
| POST | `/api/sync` | 后台同步，返回 `{"task_id": "..."}` |
| GET | `/api/sync/status` | `running`、`progress`（0–100）、`message`、`last_sync_at` |
| GET | `/api/limitups?date=` | 某一天的涨停列表。不传日期用最近一个有行情的交易日 |
| GET | `/api/pool` | 最近 3 个交易日的涨停池 |
| GET | `/api/kline?code=&days=30` | 日 K。正式行情优先，缺的日期用补拉的计算 K 线补上。不会返回今天之后的 K 线 |
| GET | `/api/similarity?code=&date=&top=20` | 这一只和涨停池的对比 |
| GET | `/api/scan?date=&top=50&min_score=0` | 全市场和涨停池对比后的排行 |
| GET | `/api/realtime/top?metric=&top_n=` | 盘中监控的榜。榜前 N 只每 2 秒随请求重算，全市场每 120 秒由后台线程跑一轮。`metric` 不认识回 422，`top_n` 超出范围回 422 |
| GET | `/api/realtime/status` | 刷新节奏与口径，不触发扫描：`status`（`live` / `warming` / `unavailable`）、`phase`、`in_session`、`refresh_seconds`、`full_rounds`、`fast_rounds`、`window_mode`、`morning_freeze`、`source`、`error`。`phase` 只有 `pre_open` / `morning` / `frozen` 三个值，11:30 起算 `frozen` |
| GET | `/api/realtime/intraday-watch` | 涨幅报警榜的整份状态：池子、每只今天的涨幅与近 3 分钟跳升、报过哪些、还没确认哪些。只读最近一轮的采样结果，不在请求里跑网络轮次 |
| POST | `/api/realtime/intraday-watch/ack?stock_code=&tier=` | 点「知道了」。两个参数都不给就是全部确认；`tier` 给错档位回 422。确认只作用在今天这一天的账上 |
| POST | `/api/ai/analysis` | 起一次整榜分析（花钱）。已经有一份在跑就接着等那一份，不重复花钱。没配齐网关回 400，榜还没有可用的一份回 409 |
| GET | `/api/ai/analysis` | 整榜分析的进度与结论。内存里没有就从存档兜底，重开程序也看得见那份跑完的分析 |
| POST | `/api/ai/single/{code}` | 单只直评（花钱）。只把这一只的证据交给模型 |
| GET | `/api/ai/single/{code}` | 这一只的直评结论，连同它被 AI 推荐过的老账。从没推过是 `null`，不是 0 次 |
| GET | `/api/ai/reports?chain=&limit=20` | 存档列表。`limit` 1 到 100 |
| GET | `/api/ai/reports/{report_id}` | 某一份存档。没有这份回 404 |
| GET | `/api/ai/picks/summary` | AI 推荐的对账汇总。只读库，一次网关都不敲 |
| POST | `/api/ai/picks/check` | 把待验证的行和库里已有的行情对一遍。同样零成本 |
| GET | `/api/ai/picks?date=&verdict=&limit=200` | 推荐明细行。`limit` 1 到 500 |
| GET | `/api/ai/picks/{code}` | 这一只被推荐过的历史，带 `never_recommended` |
| GET | `/api/ai/daily-rank?date=` | 某一天那份固定榜：备好的输入、排出的名次、校验闸的计数、有没有在跑 |
| POST | `/api/ai/daily-rank/prepare` | 备好那 100 行输入。这一步是本系统自己算的，一分钱不花 |
| POST | `/api/ai/daily-rank/rank?date=` | 排出前 20 名：这一页唯一会花钱的接口。没有备好的输入回 409，没配网关回 400 |
| GET | `/api/ai/daily-rank/status` | 只问「在不在跑」，不碰库——页面上 2 秒一拍读的就是这个 |
| GET | `/api/ai/daily-rank/history?limit=15` | 最近排过的几份榜。`limit` 1 到 60 |
| GET | `/api/summaries` | 已保存的每日总结目录 |
| GET | `/api/summaries/{date}` | 某一天的总结 JSON |
| POST | `/api/summaries/{date}/generate` | 重新生成。没配 AI 时写统计版；配了则加上解读。模型失败时仍返回统计版，并带 `ai_error` |

相似度相关接口都带 `disclaimer`，原文是「相似度不是涨停概率」。`skipped` 列出没能参加打分的涨停样本和原因。

`GET /api/sync/status` 在契约字段之外，还会给出 `phase`、`done`、`total`、`saved`、`failed`。`message` 用中文说明当前步骤。

涨停列表里的 `board` 只有 `SH` 或 `SZ`。`first_limit_time` 日线里没有，所以是 null。`amount_estimated` 为 true 时，成交额是用成交量乘均价估算的。

查询 `688`/`689` 时返回 422，`detail` 是「科创板不在纳入范围」。

`GET /api/export?type=scan|similarity|pool|summary&date=YYYY-MM-DD` 返回带 BOM 的 UTF-8 文本，并在数据目录的 `exports/` 留一份副本。`similarity` 还要带 `code`。

## 涨幅报警榜的两个接口

盯的是**前一交易日**那份 AI 固定榜挑出的前 `pool_size`（20）名。阈值、档位、跳升窗口都只在这两个接口的回包里给，页面和桌面程序不各写一份常量。读它们不会敲任何上游，也不会花钱。

`status` 五个值是五件不同的事，各说各话：`watching` 在采；`pre_open`、`after_close` 时段不对，不敲上游（那两段拿到的价格是昨天收盘价）；`no_pool` 没有可盯的池子；`unavailable` 取不到快照，或数据源回的还不是今天的价。

这两个接口的 `status` 不是同一套词：`/api/realtime/status` 那一个是 `live` / `warming` / `unavailable`，报警榜这一组才是上面五个。别拿一套的判断去写另一套。报警榜里的 `session_phase` 又是第三个东西，取值和 `realtime/status` 的 `phase` 一样只有 `pre_open` / `morning` / `frozen`。

`pool.status` 说清池子缺在哪一步：`ready` 有名单；`no_previous_trade_date` 库里定不出早于今天的行情日；`absent` 那一天没跑过榜；`prepared` 只备了输入没排名次；`failed` 那次排行失败；`broken` 那份榜读不出名单。缺池时 `pool.note` 是唯一那句解释。

回包字段：

| 字段 | 是什么 |
| --- | --- |
| `trade_date`、`session_phase`、`note` | 这份账属于哪一天、现在哪个时段、一句话状态 |
| `rows[]` | 每只今天的行情：`stock_code`、`stock_name`、`change_percent`、`last_price`、`rank`、`feed_rank`、`score`、`rise_3min`、`sampled_at`、`sample_span_seconds`、`alert`、`spike`、`reason`、`no_snapshot`、`counter_evidence`。`rise_3min` 采不满窗口是 `null`，不是 0 |
| `alerts[]` / `unacked[]` | 报过的账与其中没确认的：`id`、`tier`、`tier_label`、`percent`、`rank`、`rise_3min`、`score`、`fired_at`、`acked_at`、`stock_code`、`stock_name`、`trade_date`。`acked_at` 是空串表示「还没点知道了」，不是 null；`rank`、`score`、`percent`、`rise_3min` 取不到时是 null，页面显示「—」而不是 0 |
| `pinning`、`pending_pin_codes[]` | 窗口该不该摞在最前，以及为哪几只摞着 |
| `sampling` | 后台那一拍：`rounds`、`last_round_at`、`last_round_seconds_ago`、`snapshot_date`、`today`、`source`、`error`、`thread_running` |
| 口径 | `pool_size`、`alert_tier`、`pin_tier`、`alert_percent`、`pin_percent`、`rise_window_seconds`、`rise_min_points`、`poll_seconds`、`sample_keep_seconds`、`idle_exit_seconds`、`disclaimer` |

档位标识只有两个：`alert_3pct`（涨过 `alert_percent`）和 `spike_6pct`（涨过 `pin_percent` 且 `rise_window_seconds` 内跳升 `rise_min_points` 个点以上）。要分清哪一档触发置顶，读 `pin_tier`，别把字符串抄进代码。

`POST /api/realtime/intraday-watch/ack` 回 `ok`、`acked`（真被确认掉的条数）、`remaining`（还剩几条没确认）、`pinning`、`pending_pin_codes`，并回显 `trade_date`、`stock_code`、`tier`。写库失败时 `ok` 是 `false` 并带 `error`，不是 HTTP 错误——页面上要显示「这句没记上」，不能显示成「没有报警了」。确认之后内存里那份状态就地换掉，下一次读到的 `pinning` 就是刚变化的那个事实；桌面置顶线程最迟 `poll_seconds` 之后跟着放开。

去重键是「交易日 + 代码 + 档位」，有数据库唯一约束兜底：同一只股票同一档一天只记第一次触发，之后价格再怎么动都不重复报。
