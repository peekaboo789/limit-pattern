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
| GET | `/api/pool` | 近 N 个交易日的涨停池（默认 30） |
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

## 前 20 榜单（v0.4.0：系统综合评分 + AI 解读）

公式见 `docs/formulas/`。排名只看系统综合评分，AI 是解读器。

- `POST /api/ai-ranking/generate`，正文 `{"date": "YYYY-MM-DD"}` 可省略（默认此刻 / 最新行情日）。没配 AI 也能生成（`ai_error` 说明没有模型依据）；AI 全部失败同样照常出榜。没行情 404。
- 候选：全市场（含已涨停）按 `(−最高相似度, −最高相似度, 代码)` 取前 100；喂给 AI 时已涨停段在前。
- `GET /api/ai-ranking?date=`：省略日期返回最新一次。顶层：`date`、`run_at`、`trigger`（manual / schedule / external）、`model`、`app_version`、`formula_version`（2）、`formula_text`、`similarity_weights`、`candidate_count`、`pool_dates`、`pool_size`、`ai_error`、`batches`、`unscored_count`、`dimensions`（五项贡献：pattern 35 / technical 20 / volume 15 / capital 15 / history 15）、`disclaimer`、`runs`、`items[]`。
- `items[]`：`rank`、`code`、`name`、`board_name`、`composite_score`、`level`（强关注 / 高关注 / 观察 / 一般 / 低匹配，无信号为空串，页面显示「—」）、`contributions`（五项，缺项为 null）、`risk_penalty`、`formula`（`composite_score`、`weighted_score`、`level`、`contributions`、`risk_penalty`、`missing_items`、`weight_total`、`indicators`（七个输入指标）、`model_score`、`model_gap`、`has_signal`）、`model_score`、`model_gap`（= 模型分 − 综合评分）、`prediction_score`、`terms`、`band_lift`、`pick_history`（`picks`、`hit`、`miss`、`pending`、`avg_score`、`hit_rate`、`total`、`formula`）、`reason`（模型依据）、`risk`、`tags`、`max_similarity`、`avg_similarity`、`sample_count`、`high_similarity_count`、`band`、`is_limit_up`、`carry_rate`、`group_scores`、`best_match`、`top_matches`、`checks`。`ai_score` 保留为兼容字段，值等于 `composite_score`。
- `GET /api/ai-ranking/dates`：`{"dates": [...]}`，新的在前。
- `GET /api/ai-input?format=txt|md`：导出前 100 给外部 AI；每行带系统综合评分、等级、五项贡献、风险扣分和四个指标。
- `POST /api/ai-ranking/import`：外部 AI 返回 `{"items":[{"code","score","reason","risk","tags"}]}`；系统为这些代码算综合评分并排序，外部 `score` 记为 `model_score`，显示 `model_gap`。旧格式（`sub`、`risk_deduct`）仍能解析，`sub` 取均值当模型分。
- `GET /api/export?type=ai_ranking&date=`：TXT，列为综合评分、等级、五项贡献、风险扣分、模型分、偏差、预测分等。

## 相似度字段（v0.4.0）

- `GET /api/scan`：`items[]` 的 `score` = `max_similarity`，另有 `avg_similarity`、`sample_count`、`high_similarity_count`、`band`（高度 / 较高 / 一般 / 低 / 弱）、`top_sample`、`is_limit_up`、`change_percent`、`carry_rate`、`history_limit_up_rate`、`group_scores`、`best_match`、`top_matches`；顶层另有 `target_skips`、`rank_key`。
- `GET /api/similarity`：`stats`（四个数 + 档位），`matches[]` 带 `score`、`euclidean_score`、`cosine_score`、`band`、`group_scores`（七组，只展示）。
- 设置：`euclidean_similarity_weight`（默认 0.8）、`cosine_similarity_weight`（0.2，两者和须为 1）、`similarity_threshold_high`（默认 90，60–100）。

## 实时监控

- `GET /api/monitor`：`date`、`market_open`、`status_text`（交易中 / 休市）、`ranking_date`（被监控的上一交易日榜单日期）、`thresholds`、`source`、`error`、`updated_at`、`warn_count`、`alert_count`、`items[]`（`rank`、`code`、`name`、`ai_score`、`reason`、`price`、`pre_close`、`pct`、`level`：normal / warn ≥3% / alert ≥6%）。
- `GET /api/monitor/events?since=<id>`：当天新事件，每只每级每天一次。`last_id` 用作下一次的 `since`。只在交易时段记录。

## 交易日历、快照、回测、观察池

- `GET /api/calendar?year=`：`holidays`、`estimated`（2027 为推算）、`today_is_trading_day`、`market_open`、`previous_trading_day`、`next_trading_day`。数据目录 `holidays.json`（`{"closed": [...], "open": [...]}`）可覆盖。
- `POST /api/ai-ranking/generate` 正文可加 `"snapshot": "morning"`：下午用 11:30 冻结快照。返回里 `snapshot.kind` 为 intraday / morning_freeze / close / daily。
- `GET /api/snapshot/status`：此刻会用哪种快照，以及今天的 11:30 冻结快照。
- `GET /api/backtest?kind=similarity&top=100`：统计全部候选的下一交易日；分档 `bands[]`（90~100 / 80~90 / 70~80 / 60~70 / <60，每档 `sample_count`、`limit_up_count`、`enough`（≥50 条）、`band_lift`（相对基准倍数，样本不足为 null）、`limit_up_rate`、`up3_rate`、`avg_next_pct`）、`baseline_rate`、`min_band_observations` 与逐日 `days[]`。`kind=ai_ranking` 看每份榜单的下一交易日。
- `GET /api/next-day-pool?top=30&date=`：观察池，每只附 `band_range`、`band_samples`、`band_limit_up_rate`、`band_up3_rate`、`prediction_score`（0.5×最高相似度 + 0.3×相似度加权连板率 + 0.2×档位倍率项，缺项摊权重）、`terms`、`band_lift`、`carry_rate`；顶层 `formula`、`baseline_rate`。
- 导出类型新增 `next_day_pool`、`backtest`。

## 同步任务和数据源测试

- `POST /api/sync`：`{"task_id", "already_running"}`。同一时间只跑一个同步，重复请求返回正在跑的任务。
- `GET /api/sync/status`：另有 `current_source`、`errors`（最近 10 条）、`sources`（每个源的连续失败次数、是否暂停）、`cancelling`，`phase` 可能是 cancelled / failed。
- `POST /api/sync/cancel`：取消正在跑的同步（已写入的保留）。
- `POST /api/sources/test`，正文 `{"timeout": 8}`：并行检查三个源（各拉 600000、000001 近 10 天日线），每个源最多等 timeout 秒，返回 `ok`、`latency_ms`、`latest`、`error`。
- 换源 `PUT /api/settings {"data_source": ...}` 立即保存；同步进行中，后续股票按新设置取源。
- 每次请求连接 5 秒、读取 10 秒超时；单个源连续失败 3 次（或返回 5xx/429）暂停 90 秒；拉股票列表总时限 45 秒；8 个并发。

## POST /api/ai/test

测试 AI 接口。正文可带未保存的 `ai_base_url`、`ai_model`、`ai_api_key`（空则用已保存值）。
返回 `{ok, reply?, error?, endpoint, note, latency_ms}`；错误是具体原因（连接超时、读取超时、域名解析、SSL 证书、代理、HTTP 状态码和返回内容），不含密钥。
地址填到 `/v1` 即可；阿里云百炼只填域名时自动补 `/compatible-mode/v1`。自动使用 Windows 系统代理和系统证书库（truststore）。
AI 排名按每批 25 只、紧凑表格分批请求，单批读取超时 150 秒。

### 深度思考（v0.2.3）

设置项 `ai_thinking`: `auto`（默认，不发送 enable_thinking）/ `on` / `off`。返回 400 且提到 enable_thinking 时自动换另一个值重试一次，并按「地址|模型」记到 `ai_thinking_learned`。
思考开启时百炼走 `stream=true`，只收 `delta.content`，跳过 `reasoning_content`；单批读取超时 300 秒。
AI 排名每批 20 只、最多 3 批并发，进度见 `GET /api/ai-ranking/progress`。
