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
| GET | `/api/summaries` | 已保存的每日总结目录 |
| GET | `/api/summaries/{date}` | 某一天的总结 JSON |
| POST | `/api/summaries/{date}/generate` | 重新生成。没配 AI 时写统计版；配了则加上解读。模型失败时仍返回统计版，并带 `ai_error` |

相似度相关接口都带 `disclaimer`，原文是「相似度不是涨停概率」。`skipped` 列出没能参加打分的涨停样本和原因。

`GET /api/sync/status` 在契约字段之外，还会给出 `phase`、`done`、`total`、`saved`、`failed`。`message` 用中文说明当前步骤。

涨停列表里的 `board` 只有 `SH` 或 `SZ`。`first_limit_time` 日线里没有，所以是 null。`amount_estimated` 为 true 时，成交额是用成交量乘均价估算的。

查询 `688`/`689` 时返回 422，`detail` 是「科创板不在纳入范围」。

`GET /api/export?type=scan|similarity|pool|summary&date=YYYY-MM-DD` 返回带 BOM 的 UTF-8 文本，并在数据目录的 `exports/` 留一份副本。`similarity` 还要带 `code`。

## AI 前 20 榜单

- `POST /api/ai-ranking/generate`，正文 `{"date": "YYYY-MM-DD"}` 可省略（默认最新行情日）。没配 AI 返回 409，AI 出错 502，没行情 404。
- `GET /api/ai-ranking?date=`：省略日期返回最新一次。字段：`date`、`run_at`、`trigger`（manual / schedule）、`model`、`candidate_count`、`pool_dates`、`pool_size`、`disclaimer`、`runs`、`items[]`（`rank`、`code`、`name`、`board_name`、`ai_score`、`reason`、`similarity`、`shape_score`、`volume_score`、`volume_pct_score`、`best_match`、`close`）。
- `GET /api/ai-ranking/dates`：`{"dates": [...]}`，新的在前。
- `GET /api/export?type=ai_ranking&date=`：TXT。

## 实时监控

- `GET /api/monitor`：`date`、`market_open`、`status_text`（交易中 / 休市）、`ranking_date`（被监控的上一交易日榜单日期）、`thresholds`、`source`、`error`、`updated_at`、`warn_count`、`alert_count`、`items[]`（`rank`、`code`、`name`、`ai_score`、`reason`、`price`、`pre_close`、`pct`、`level`：normal / warn ≥3% / alert ≥6%）。
- `GET /api/monitor/events?since=<id>`：当天新事件，每只每级每天一次。`last_id` 用作下一次的 `since`。只在交易时段记录。
