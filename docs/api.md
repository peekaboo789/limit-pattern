# 接口

基址 `http://127.0.0.1:8765`。成功时直接返回 JSON。失败时 HTTP 4xx/5xx，正文是 `{"detail": "中文说明"}`。日期一律 `YYYY-MM-DD`，股票代码 6 位。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | `status`、`version`、`data_date`、`data_source`、`active_source`、`exports_dir` |
| GET | `/api/settings` | 保留天数、对比窗口、AI 是否启用、地址、模型、密钥是否已填、数据源（`auto` / `eastmoney` / `tencent` / `sina`）、实际使用的源、纳入范围 `market_scope`、`excluded_boards`、上次同步、上次清理。另给 `ai_active_*`（id / 名称 / 打码地址 / 模型 / 密钥有没有）与 `ai_source_count`：那三格是**表单要编辑的**，`ai_active_*` 是**下一次请求真正用的**，两者可以不是同一家 |
| PUT | `/api/settings` | 修改可写项。`ai_api_key` 只写入、不回显。对比窗口传了非 3 会 422 |
| POST | `/api/sync` | 后台同步，返回 `{"task_id": "..."}` |
| GET | `/api/sync/status` | `running`、`progress`（0–100）、`message`、`last_sync_at`、`last_sync_sources`（上一轮的出数来源账） |
| GET | `/api/limitups?date=` | 某一天的涨停列表。不传日期用最近一个有行情的交易日 |
| GET | `/api/pool` | 最近 3 个交易日的涨停池 |
| GET | `/api/kline?code=&days=30` | 日 K。正式行情优先，缺的日期用补拉的计算 K 线补上。不会返回今天之后的 K 线 |
| GET | `/api/similarity?code=&date=&top=20` | 这一只和涨停池的对比 |
| GET | `/api/scan?date=&top=50&min_score=0` | 全市场和涨停池对比后的排行 |
| GET | `/api/realtime/top?metric=&top_n=` | 盘中监控的榜。榜前 N 只每 2 秒随请求重算，全市场每 120 秒由后台线程跑一轮。`metric` 不认识回 422，`top_n` 超出范围回 422。每行带 `similarity_band`、`next_day_rate_percent`、`next_day_rate_samples`，整份回包带 `probability`（底表，见「历史回测与次日预测池」） |
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
| GET | `/api/ai-sources` | 大模型网关清单：每条的名称、模型、**打码后的**地址、密钥有没有、是否 `complete`、哪一条 `active`，加 `file_path`、`max_sources`、`custom_count`（占格子的条数，内置那行不算）与「文件读坏了」那句话。密钥永不回传 |
| POST | `/api/ai-sources` | 加一条网关（`name`/`base_url`/`model`/`api_key`）。**不会顺手把它设成当前**；超过 20 条、名称超 40 字、带 `***` 的地址都回 422 |
| PUT | `/api/ai-sources/{source_id}` | 改一条，留空的列沿用原值；全留空 422。内置那条「设置页里那三项」不给改（422） |
| DELETE | `/api/ai-sources/{source_id}` | 删一条。正在生效的那条要先切走（422），内置那条不给删（422） |
| PUT | `/api/ai-sources/{source_id}/active` | 切换当前生效网关。当场生效、活过重启，整榜 / 单股直评 / 每日固定榜三条花钱链路一起跟着走。**这一步不敲网关** |
| GET | `/api/ai-memory/summary` | 对账总账（判据日、定判数、命中率）+ 两份快照目录的现状与合计大小 + 这份快照自己的 `field_gaps`。只读库，零成本 |
| GET | `/api/ai-memory/days` | `model_memory/` 与 `failure_logs/` 各自已经有哪些日子，带每个文件的字节数与落盘时刻 |
| POST | `/api/ai-memory/export?date=` | 生成或重生成某一天的两份 JSON（临时文件 + rename）。多点一次只是重写同一天，不多出一份样本，也不碰别的日子。库里没行情 404，未来日期 400 |
| POST | `/api/ai-memory/run?date=` | 手动跑收盘后那条闭环：对账 → 导当天 → 回填上一交易日。与每日流水线调的是同一个函数。失败不包成 500，半截状态原样回 `errors` |
| POST | `/api/backtest/run?start=&end=&split_ratio=&max_days=` | 起一轮回测（后台线程）。不传区间吃库里全部已同步行情；`split_ratio` 0.1 到 0.9；`max_days` 1 到 60，用来先跑短一段。区间反了回 422，区间里没行情回 404。回 `task_id`、`already_running`、`days_total`、`running`、`progress`、`message`、`run_id` |
| GET | `/api/backtest/status` | 后台那一轮跑到哪了：`running`、`progress`、`phase`、`message`、`task_id`、`days_done`、`days_total`、`current_date`、`error`、`run_id`。不碰库 |
| GET | `/api/backtest/latest` | 最近一次**已完成**的回测（`run`，没有是 null）、概率列底表（`probability`）、最近 20 行台账（`runs`）、`disclaimer` |
| GET | `/api/backtest/runs?limit=20` | 回测台账，含 `running` / `failed` 的行。`limit` 1 到 100 |
| GET | `/api/backtest/runs/{run_id}` | 某一轮的完整账：分档、校准、泄漏自检、逐日、参数。没有回 404 |
| GET | `/api/backtest/observations?run_id=&band=&code=&limit=&offset=` | 观测明细分页。`band` 用 `90~100` 这种标签；`limit` 1 到 1000（服务端再夹到 `OBSERVATION_PAGE_MAX`） |
| DELETE | `/api/backtest/runs/{run_id}` | 删一轮账与它的明细。派生数据，不碰原始行情。没有回 404 |
| GET | `/api/prediction/pool?date=&size=200` | 次日预测池：当日未涨停、按形态/连板/档位三项历史证据排序。`date` 不传取库里最近交易日，未来日期回 400，没行情回 404。`size` 1 到 1000 |
| GET | `/api/quality/summary` | 质量账表的总账：error / warning 各多少条、十四项检查各多少条、最晚记到哪天、上次检查时刻，加上「能不能删」（`purge`）与十四项检查的名字、中文说法、error/warning 归属（`checks`） |
| POST | `/api/quality/check?start=&end=&max_days=` | 跑一遍十四项检查。**只写账表，一个行情字段都不改**。不传区间就查库里现存的那段日期 |
| GET | `/api/quality/errors?date=&severity=&check=&limit=200` | 账目明细。`severity` 只能是 `error` / `warning`，`check` 用检查项的名字（`ohlc_check` 那种），给错都回 422 |
| POST | `/api/quality/purge?start=&end=&confirm=` | 删掉 error 级日线与它们的下游。**默认只算不删**（`dry_run=true`），要真删必须带 `confirm=true` |
| GET | `/api/data/status` | 数据管理页概况：七张表各多少行、四类派生数据按日覆盖多少、样本池日期、质量账计数、训练统计、跳过账分阶段行数、当前任务 |
| GET | `/api/data/job` | 后台重建任务的进度与记账。页面每 2 秒轮询这一条，不靠重进页面刷新 |
| POST | `/api/data/rebuild-features?start=&end=&codes=` | 重算单日特征。区间留空吃库里全部已同步交易日；`codes` 是逗号分隔的 6 位代码 |
| POST | `/api/data/rebuild-patterns?start=&end=` | 重建涨停样本库。留空只重建样本池该覆盖的那 3 个交易日 |
| POST | `/api/data/scan-market?start=&end=` | 全市场匹配扫描，明细与汇总一起落库。留空只扫最新一个交易日 |
| POST | `/api/data/build-training?start=&end=&codes=` | 构造训练宽表。闸门吃的是账表里的 error，跑之前先跑一遍 `POST /api/quality/check` |
| GET | `/api/data/skips?stage=&reason=&date=&limit=200&offset=0` | 跳过账分页：哪一天哪只股票在哪个阶段因为什么没进库。`limit` 1 到 2000 |
| GET | `/api/patterns/overview?date=` | 涨停形态页：样本池这几天库里存了哪些样本、各天被闸门跳过了什么。只读库，不现算 |
| GET | `/api/patterns/top?date=&limit=50` | 样本池已落库的样本清单，按涨停日、代码排，只给概要字段。`limit` 1 到 500 |
| GET | `/api/patterns/{code}?with_details=true` | 这一只股票在保留窗口里落库的涨停样本，带三日明细与向量尾部那 7 项聚合。一条都没有回 404 |
| GET | `/api/matches/summary?date=&limit=100&offset=0` | 某一天落库汇总的分页，按最高相似度排。`limit` 1 到 1000 |
| GET | `/api/matches/{code}?date=` | 这一只股票某一天**库里**的匹配明细。没落库回 `ok: false` 并说清为什么，不偷偷现算 |
| POST | `/api/matches/{code}?date=` | 现算这一只的匹配明细并落库，与全市场扫描共用同一把尺子 |
| GET | `/api/matches/compare?code=&sample_code=&date=&sample_date=` | 左右对比：目标当天的三日窗口与那条涨停样本落库的三日窗口，逐字段差值。`sample_date` 必填 |
| GET | `/api/matches/export?date=&code=&top=200` | 匹配明细 CSV。`top` 是按最高相似度取前几只，不是前几行 |
| GET | `/api/summaries` | 已保存的每日总结目录 |
| GET | `/api/summaries/{date}` | 某一天的总结 JSON |
| POST | `/api/summaries/{date}/generate` | 重新生成。没配 AI 时写统计版；配了则加上解读。模型失败时仍返回统计版，并带 `ai_error` |

相似度相关接口都带 `disclaimer`，原文是「相似度不是涨停概率」。`skipped` 列出没能参加打分的涨停样本和原因。

`GET /api/sync/status` 在契约字段之外，还会给出 `phase`、`done`、`total`、`saved`、`failed`。`message` 用中文说明当前步骤。

`last_sync_sources` 是上一轮同步留下的出数来源账：`at`、`active_source`、`chain`、`sources`（每个源各出了多少只）、`saved`、`failed`、`switched`、`quality_errors`、`quality_warnings`、`quality_scanned_days`、`quality_skipped`。从没跑完过一轮时它是 `null`，不是空对象——「查过且没问题」和「没查过」不能读成同一个数。

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

## 历史回测与次日预测池

回测回答的是「相似度落在某一档的股票，下一个交易日真的涨停的比例是多少」。它扫的是库里已经发生过的行情，一天等于一轮全市场打分，所以区间越长越慢。跑成的账留在 `backtest_runs` 与 `backtest_observations`，盘中榜的概率列和预测池的档位项都读同一份账。

### 一轮回测怎么跑

`POST /api/backtest/run` 先校验（区间反了 422、库里没行情 404），然后在后台线程里逐日重算样本池、逐日给当天全市场打分、再取下一交易日的涨停标签。启动前先落一行 `status=running` 的占位，跑成改成 `done`，跑砸改成 `failed` 并在 `error` 写下原因、`note` 写成「跑砸了：…」。这一行不会被事务回滚掉，所以「砸过几次」数得出来。进程在跑的时候退出，下一次启动会把残下的 `running` 行记成 failed（「进程重启，这一轮没有跑完」），不会永远挂着「正在回测」。

同一时刻只跑一轮：已经有一轮在跑时再 POST，回的是那一轮的 `task_id` 和 `already_running: true`，不起第二个线程、不重复扫。

分档边界固定为 `90~100`、`80~90`、`70~80`、`60~70`、`<60`（`core/constants.py`，页面从回包读，不各写一份）。每一档给 `sample_count`（**已经有结果**的观测数）、`limit_up_count`、`limit_up_rate`、`baseline_rate`、`lift`、`pending_count`。区间最后一天没有下一交易日，那些观测的 `next_limit_up` 是 null，进 `pending_count`——把没揭晓的当成没涨停，等于用未来的空白压低历史发生率。一档一条有结果的观测都没有时 `limit_up_rate` 是 null（页面显示「—」），不是 0%。

`split_ratio` 按**交易日**切前后两段：前半段拟合「档 → 发生率」，后半段检验。不切观测是因为同一天的行情互相高度相关。检验段给 `brier`、`log_loss`、`ece`（越小越好）、等宽分箱 `bins`，以及按档位口径的 `band_checks`（`band`、`predicted`、`observed`、`count`）。有结果的交易日不足 2 天、或观测不足 20 条时 `available` 是 false、`reason` 写明差在哪，指标是 null 不是 0。

`leakage` 是四条结构判据加三条抽验重算：形态窗口最后一天不晚于评估日、窗口不碰标签日、标签日严格晚于评估日、涨停样本窗口严格早于涨停日；抽验走首页那条 `similar_to` 重算，分数必须和落库一致（差 ≤ 0.01）。样本池条数不同时只登记、不判红（回测不补拉、剔 error 样本，和首页那条路本来就有这两处刻意的差别），条数一致而分数不一致才算尺子漂了。

### 概率列与档位底表

`GET /api/backtest/latest` 的 `probability` 就是盘中榜那一列的底表：

| 字段 | 是什么 |
| --- | --- |
| `rates` | 每个档位标签一项：`percent`（百分数）、`samples`、`lift`。这一档没有发生率、或观测数不足 `MIN_BAND_OBSERVATIONS`（50）条时 `percent` 是 null |
| `floors` | 四个档位边界 |
| `run_id`、`start_date`、`end_date`、`day_count`、`observation_count`、`baseline_percent` | 这份数来自哪一轮回测 |
| `min_band_observations` | 门槛（50） |
| `note` | 一句人话：来自第几次回测、哪个区间、哪个基准，哪几档因样本不足留空 |

一轮榜的所有行从同一次 `probability_table` 取值，不逐行查库——不然读到一半有人跑完新回测，榜上前半是旧发生率、后半是新发生率。整张表读不到已完成的回测时 `rates` 是空对象、`run_id` 是 null、`note` 说「概率列整列留空（不是 0%）」。跑过回测但一档统计都没落下来（那几天建不出涨停样本池）也照样留空，并在 `note` 里说清是哪一轮。

`/api/realtime/top` 每行的 `next_day_rate_percent` 是「这一行最高相似度所在档」的次日实际涨停率，`next_day_rate_samples` 是那一档的观测数。三种留空分开算但结果一样：档位观测不够门槛、这一档在回测里没出现过、整张底表是空的——都是 null，页面画成「—」。`0.00%` 是一个有内容的结论（这一档历史上从没涨停过），不能拿来冒充「不知道」。

### 次日预测池

`GET /api/prediction/pool` 是纯读加现算，不写库：候选是基准日「还没涨停」的全市场，按三项历史证据加权排序。

1. `shape`：当日最高相似度（0~100）。权重 0.5。
2. `carry`：与它最像的那批涨停样本，在涨停之后第二个交易日继续涨停的比例，按相似度加权（90 分的样本比 65 分的说话更响）。权重 0.3。
3. `band`：档位历史发生率相对全样本基准的倍数，按 `PREDICTION_LIFT_CAP`（2 倍）封顶，再折成 0~100。权重 0.2。

缺了的项（没有回测、没有可用连板样本）把权重摊给其余项，不回一个 0。每行的 `score_terms` 给三个原值，`prediction_score` 是加权后的分。

连板证据有唯一一道未来信息闸门：样本涨停日的下一交易日必须**严格早于**基准日。等于或晚于基准日的一律不查。已知后果也写在回包里：样本池只有三个交易日，基准日当天涨停的那批样本永远进不了连板统计，`carry_known_count` 为 0 的行照实显示。

回包字段：

| 字段 | 是什么 |
| --- | --- |
| `pool_date`、`next_trade_date` | 挑选日与预测目标日。目标日先问交易日历；日历可能比行情新（当天刚收盘、行情还没同步），这时日期说得出、结果仍只用已经发生的行情 |
| `pool_size`、`pool_total`、`pool_dates`、`sample_pool_size` | 取了几名、当日参与打分多少只、样本池的交易日、样本池条数 |
| `skipped_samples`、`missing_bars`、`dirty_skipped` | 建不出窗口的样本（最多 20 条）、凑不齐三日窗口的候选数、被 error 脏行挡掉的候选数 |
| `rows[]` | `stock_code`、`stock_name`、`board`、`board_label`、`is_st`、`pool_rank`、`rank`、`max_similarity`、`avg_similarity`、`sample_count`、`high_similarity_count`、`high_similarity_rate`、`band`、`band_rate`、`band_samples`、`band_lift`、`carry_known_count`、`carry_hit_count`、`carry_missing_count`、`carry_rate`、`close`、`change_percent`、`volume`、`turnover_rate`、`prediction_score`、`score_terms`。取不到的数是 null，不是 0 |
| `bands[]` | 档位底表，和 `probability` 同一份口径 |
| `baseline_rate`、`calibrated`、`backtest_run_id`、`backtest_day_count`、`backtest_observation_count`、`min_band_observations` | 这一页吃的回测账。没跑过回测时 `calibrated` 是 false、`backtest_run_id` 是 null |
| `note`、`formula`、`disclaimer` | 这一页凭什么、怎么合成分、免责声明原文 |

预测分是历史统计的加权，不是涨停概率，也不是收益预测。这一页不给「建议买入」。

## 数据质量闸门

十四项检查只读、只往 `data_quality_errors` 写账；删数据是 `POST /api/quality/purge` 的活，而且默认不删。
每一项都有 error 和 warning 两种归属：**只有 error 会被下游当作拦路条件**（`blocked_codes(session, day)` 就是给建样本、
构造训练集那一类动作问的），warning 只记账。这么分是因为公开源的日线字段完整度不一样——腾讯、新浪经常只给量不给额，
把「没有成交额」判成 error 等于把整个源判死，那会把闸门用成删数据的刀。

| 检查项 | 中文说法 | 归属 |
| --- | --- | --- |
| `code_check` | 代码不是六位数字 | error |
| `market_check` | 代码前缀与库里的交易所不一致 | error |
| `security_type_check` | 板块判不出来 | warning |
| `name_check` | 股票名称为空 | warning |
| `trade_date_check` | 行情日期落在未来或周末 | error |
| `ohlc_check` | 开高低收缺项、非正数或高低关系不成立 | error |
| `volume_check` | 成交量缺项或为负 | error |
| `amount_check` | 成交额为负 | error |
| `duplicate_check` | 同一只股票同一天有多行 | error |
| `missing_check` | 这只股票在这一段里缺了行情日 | warning |
| `change_percent_check` | 当日涨跌幅超出该板块通用涨跌停幅度 | error |
| `code_name_match_check` | 库里的名称与本轮股票列表对不上 | warning |
| `universe_size_check` | 这一天有行情的股票数明显少于近期水位 | error |
| `pre_close_continuity_check` | 前收盘价接不上上一根的收盘价 | error |

`GET /api/quality/summary` 的 `checks` 数组就是这张表的机器版本，页面照着它渲染列头，别把中文说法抄进代码。

三个判据写死在 `core/constants.py`，页面上没有开关：涨跌幅超出板块通用幅度再放 `QUALITY_CHANGE_TOLERANCE_PERCENT`（1.5 个百分点）
才算脏——公开源的四舍五入会做出 10.03% 这种数；前收盘价与上一根收盘价的绝对差超过 `QUALITY_PRE_CLOSE_TOLERANCE`（0.02 元）
算接不上，且只在两根线真的挨着（间隔 ≤ 8 天）时比，中间隔着长假或停牌段接不上不是脏数据；一天有行情的股票数低于近期中位数的
`QUALITY_UNIVERSE_MIN_RATIO`（60%）判 `universe_size_check`，库里股票数不足 50 只时这一项直接跳过并写进日志（首启、测试都属这种，
说「跳过了」比给一个假通过值钱）。

回包 `written` 是新写的条数，`existing` 是「已经记过、这次跳过」的条数。同一个「日期 + 代码 + 检查项」只记一次、不累加：
每轮同步都会重写同一批行，累加只会让一个一直没修的问题在页面上越看越严重。`skipped_checks` 说明这一趟哪些项没做成——
`code_name_match_check` 只有同步那一趟做得成（股票列表是它自己下的），手动调 `POST /api/quality/check` 时它回的不是「通过」而是
「名称核对没做成」。空库回 `ok: true` 但 `scanned_days`、`scanned_rows` 都是 0，并在 `skipped_checks` 里写明区间里没有行情行。

### 清理接口的三道闸

`POST /api/quality/purge` 的 `planned` 先给数字：`daily_quotes`、`calc_bars`、`daily_features`、`skipped_records`、
`limit_up_patterns`、`pattern_matches`、`data_quality_errors` 各会带走多少行。不带 `confirm` 时 `dry_run` 是 `true`、
`deleted` 是空对象，一个字节都不动。

第一道闸是 `confirm`。第二道是比例：要删的日线超过区间内行情行数的 `QUALITY_PURGE_MAX_RATIO`（5%）时回
`ok: false` 与 `refused`，一条都不删——那不是「清理几条脏行」，而是检查本身跑坏了（比如某个源整段只回了半天表），
这一刀砍下去动的是原始行情红线。第三道是删除范围：只有 error 级账目指到的「哪只哪天」会被删，`error_pairs` 不含
没有日期的账（名称对不上）和没有代码的账（整天的股票数水位），所以这两种永远不会按天删掉主档。

跟着一起删的是下游派生行：补拉的计算 K 线、特征行、跳过记录，样本行（涨停那天自己脏，或它那三天形态窗口里坐着被抽掉的一根——
这种向量再也复现不出来），匹配行两头都算（目标那天脏了结果不成立，样本那天脏了分数是抄来的），以及指向被删那一行的 error 账本身。
正式日线以外的东西都可以重算，不用备份。

删完在同一趟里复查**一遍**，新暴露的 error 放进 `newly_exposed` 报出来，然后停手。中间那根被抽掉之后，后面那根的
`pre_close` 就接不上了，那是删除动作造出来的缺口、不是原因；照着它再删一轮会顺着时间轴把历史啃穿。第二轮交给下一次同步或人工判断。

## 派生数据的四条重建线

「派生数据可以删、原始行情才是红线」在这一节落地成四个后台任务：单日特征、涨停样本库、全市场匹配、训练宽表。
四个都不读网络，只吃库里已有的日线与补拉来的计算 K 线，跑砸了重跑一次就行。

`POST /api/data/rebuild-features`、`/rebuild-patterns`、`/scan-market`、`/build-training` 形状一样：起线程、回
`{"task_id": "...", "label": "全市场匹配扫描", "days_total": 5, "running": true, "progress": 0, "message": "...", "error": ""}`，
进度靠轮询 `GET /api/data/job`。区间留空时各任务覆盖面不同，写死在 `data_service.DEFAULT_DAY_SCOPE`：特征与训练吃库里
**全部**已同步交易日（装了两周历史的库要能整段重算，而不是只重算最新那天），样本只重建涨停池那 3 个交易日，
扫描只扫最新一个交易日。

日期必须在请求还活着的时候定下来。「区间里没有已同步的交易日」这种话一旦落到后台线程，页面上就只剩一句「失败」；
所以 `data_service._days_for` 先在校验用的那一眼里把日期挑完。空库回 404「库里还没有行情，先执行同步」，
区间反了或区间里没有交易日也当场报错（422 / 404），连线程都不起。

槽位只有一个。这些活都在写 SQLite，两个请求一起把写锁撞成 `database is locked` 的话两边都 500、用户完全不知道发生了什么；
所以现在第二个任务直接 409，消息里写清是谁在占用这个槽位（「后端任务「重建涨停样本」正在运行，请等它跑完」）。
`GET /api/data/status` 里的 `running_job` 给的就是这个名字。

`GET /api/data/job` 的 `result` 是这一轮的记账，`counts` 是任务跑完之后**重新数库**得到的行数。两份都要给：
记账里的 `stock_count`、`pool_size`、`universe_count` 是单日快照，区间任务加不出总量（三天加出来的「样本池 180 条」
没人认得），而页面上那句「这一轮之后库里有多少行」必须由库里数。哪些键跨天累加、哪些键保持最后一轮取值，
登记在 `ADDITIVE_KEYS` / `SNAPSHOT_KEYS` 两张表里，不靠 `if key in (...)` 那种写一处漏一处的判断。

`GET /api/data/status` 是数据管理页的概况：七张表各多少行（`counts`）、样本/特征/汇总/训练四条按日覆盖曲线
（`patterns_by_date` 那一类，同一个 `_group_count` 出的数）、样本池日期（`pool_dates`）、质量账（`quality`）、
训练统计（`training`）、跳过账分阶段行数（`skips.by_stage`）。缺就是 0，没跑过就是空——「今天有几条样本」读的是
`limit_up_patterns`，不是现算的涨停池，否则用户点下重建之后数字变了，等于页面之前报的是一个不存在的数。

### 闸门怎么挡样本，挡了多少在哪看

`build_patterns` 对每一只当日涨停的股票问一次 `blocked_codes(session, day)`：涨停当天自己命中 error，
或它那三天形态窗口里坐着被抽掉的一根，就不建样本，往 `skipped_records` 记一行 `stage="pattern"`。
两种原因分开记账，说法由 `pattern_service.REASON_LABELS` 一份说了算，页面上直接读 `reason_labels`：
`insufficient_window` 是「算不了」（库里凑不齐涨停前三根 K 线），`data_quality_error` 是「算得出但不该用」。
注意 error 挡的是**双向**的：脏行自己那天的样本建不成，后面任何一天的窗口里含它也一样建不成。

`GET /api/patterns/overview` 的 `days[].skipped` 就是这几天各挡了多少，明细去 `GET /api/data/skips`：
可按 `stage`（`feature` / `pattern` / `match` / `training`）、`reason`、`date` 筛，`limit` 1 到 2000。
训练宽表那一条线还有第三种记账：`quality_passed` 为 false 的行**照样落库**、带着 `reason`，
因为「被闸门挡过」本身是训练时要知道的事；它进的是 `blocked_rows`，不是消失。

## 涨停形态样本与匹配明细

`GET /api/patterns/overview?date=` 读的是 `limit_up_patterns`，不是现算的涨停池——这两份数据在库里可以不一致
（样本没重建、当天有 error 被挡、行情不足三天），而页面要展示的正是「库里有的那一版」。回包给
`trade_date`、`pool_dates`、`pool_is_current`（样本池跟当前涨停池对不对得上，对不上页面就提示先重建）、`sample_total`、
`days[]`（每天 `limit_date` / `count` / `skipped` / `items`）、`feature_names`、`aggregate_names`、`dimension`（67）、
`reason_labels`、`disclaimer`。`date` 落在没有行情的一天回 404「这一天没有行情」，不是回一张空表。

`GET /api/patterns/{code}` 给这一只股票在保留窗口里落库的全部样本，按涨停日倒序，带三日明细与向量尾部那 7 项聚合
（`aggregates`）——页面上「三日涨幅、量能趋势」那几个数要能指回向量里的维度，而不是前端再算一遍。

打分只有一把尺子：`backtest_service.score_day`。全市场扫描、单只现算、回测逐日重算、预测池的证据行都调它，
差异只在 `only_codes` 和 `top_matches` 传什么。所以「页面上看到的分」和「回测里统计的分」不存在两套算法。

- `GET /api/matches/{code}` 只读库、不现算：这一页展示的必须和上一轮扫描落下去的是同一份数。库里没落过回
  `ok: false`，`reason` 说清是没扫描还是那天没行情。
- `POST /api/matches/{code}` 现算那一只并落库。
- 两条的失败回包键与成功回包逐项对齐（`items` / `summary` / `pool_dates` / `pool_size` / `skipped_samples` 都在），
  页面只需要按 `ok` 分支，不用为「读库没读到」和「现算没算出」写两套兜底。
- `GET /api/matches/compare` 的 `sample_date` 必填到具体涨停日：同一只股票一周里可以涨停三次，只给代码会拿到
  不知道哪一天的窗口，页面上那两个数也就不是一对。
- `GET /api/matches/export` 的 `top` 是按最高相似度取前**几只**，不是前几行；截断时 `X-Export-Truncated: 1`，
  文件名尾巴带 `_截断`。正文是 UTF-8 BOM（Excel 直接双击不花屏）。下载头里 `filename=` 那段必须是纯 ASCII——
  中文写进去会让响应头编不出 latin-1，整个导出接口 500，页面上点「导出 CSV」只得到一个服务器错；
  真名字走 `filename*=UTF-8''…`。

相似度分项里，成交额那一维**不参与总分**（总分是形态 0.6 + 量能 0.25 + 量比 0.15），只单独出格子。
两边这一组维度一个数都没有时（新浪那一路 `amount` 整列为空，三个成交额特征只能补 0），零向量对零向量会被
`score_matrix` 读成满分 100——那不是「成交额很像」，那是没数据，所以算法层标成 NaN、出口处
`similarity.percent()` 换成 `null`。NaN 不能带出这一层：`json.dumps` 会把它写成裸的 `NaN`，
浏览器 `response.json()` 当场解析失败，整个页面变成空白而不是一格空值。

## 形态聚类与向量检索

`GET /api/clusters/overview?date=&k=&include_members=` 分组的对象是**库里已落库的涨停样本**
（`limit_up_patterns` 在样本池那三天里的可用行），不是现算的涨停池——页面上每个组都要能指回样本库里具体的几行。
回包给 `trade_date`、`pool_dates`、`sample_count`、`ignored_stale`（向量长度对不上 67 的旧口径行数，跳过并如实报数）、
`k`、`selected_by`（`轮廓系数最大` / `调用方指定` / `样本不足，不分组`）、`silhouette`、`evaluated[]`、
`clusters[]`（`cluster_id` / `name` / `size` / `share` / `description` / `intra_distance` / `nearest_other_distance` /
`separation` / `signature` / `representative` / `members[]`）、`points[]`（散点图，样本各一点、每组一个质心）、
`high_band`、`k_min`、`k_max`、`disclaimer`。

三条硬约束写在这个模块的门口：

- **组名不靠人写**。`signature` 是组内样本真实特征的中位数，组名由固定词表（涨幅、量能倍数、20 日位置、振幅四档阈值）
  从这份中位数里生成，同名先补振幅档、还同名才加 `#n`。改阈值必须同步改测试。
- **聚类空间就是打分空间**。分组前先把向量搬到 `RobustScaler + 权重开方` 那个空间里，缩放参数由样本池拟合，
  权重取 `features.dimension_weights()` 归一化到和为 1——与 `similarity.score_matrix` 一个口径。
  PCA 只服务散点图，不参与任何分组或打分。
- **换手率缺列就是缺列**。公开源那一路 `day_details.features.turnover_rate` 整列可能是空的；
  整组都缺时 `signature.turnover_rate` 回 `null`、画像写「T-1 换手 —」，不能让它在中位数里落成 `0.00%`。

`GET /api/clusters/{code}?date=&k=&top_members=` 算的是「这只股票今天的三日形态最像哪一组」。
分数来自 `similarity.score_components`——与首页榜、盘中榜、回测、匹配明细同一把尺子，这一页不产生第二套分数，
也不产生任何概率。目标自己当天涨停时也在样本池里，但它那条样本**不参与**与自己这一组的比对（留着就是拿自己
跟自己打 100 分）。回包给 `groups[]`（`cluster_id` / `name` / `size` / `avg_similarity` / `max_similarity` /
`high_band_count` / `top_members[]`，按组内均分从高到低排）、`assigned_cluster_id` / `assigned_cluster_name` / `margin`、
`window`、`skipped_reason`。

算不出来时不编一个归属：`date` 落在没有行情的一天回 404「这一天没有行情」；代码不在沪深主板/创业板范围回 422；
样本池不足 `CLUSTER_MIN_SAMPLES`（4）条回 400 并报出「只有 N 条…先在数据管理页重建涨停样本」；
指定 `k` 大于等于样本数回 400「每组只剩一个样本」；目标股凑不齐三天行情则回 200，`skipped_reason` 说清原因、
`groups` 是空表。

向量检索有两种后端（`algorithms/vector_index.py`）：`numpy` 走 Gram 展开，`faiss` 走 `IndexFlatL2`。
**两者都是精确检索，换后端只换时间，不换读数**，所以后端既不在设置里、也不在查询参数里——
`FAISS_PAIR_THRESHOLD`（查询数 × 样本数）越过那条线就用 faiss，否则用 numpy。faiss 回的结果是**按距离升序排好**的，
必须用 `indices` 把每一格摆回样本原来的位置，否则每行仍是「一组正确的距离」却配错了股票；
测试里那条逐元素比对判据配了这个负向控制。实测这一批数据上两种后端的距离最大差在 `4e-6` 量级
（float32 转换那一档），换成欧氏分是 `5e-5` 分，落在两位小数与分档阈值边界之外。

## 多套网关清单与「当前生效」

清单落成一个 JSON 文件（`ai_sources.json`，跟 `settings.json` 同一个数据目录），**不是表**：
它没有行情外键、不需要跟着日期重建，也不该被 `POST /api/quality/purge` 那一类删派生数据的动作碰到。
`core/ai_sources.py` 是它唯一的读写口。

清单第一条永远是内置那行 `SETTINGS_SOURCE_ID = "settings"`，显示名「设置页里那三项」——它不是复制来的第四套配置，
而是**指向设置页那三个输入框的别名**。改那三格走 `PUT /api/settings`，改这一行走 `PUT /api/ai-sources/settings`，
两边写的是同一份 `settings.json` 字段；这一行不给删、不给改（422），也永远数在 `max_sources` 之外。

三条硬规矩：

- **密钥只进不出**。`describe()` 里每行只有 `key_present` 布尔值，地址过 `mask_url_secret()`——尾段像令牌
  （含数字）才打码成 `sk-***`，普通 `/v1` 结尾原样留着。写回时带 `***` 的地址直接 422，
  免得页面把打码回显当成真地址存一遍，把真令牌换成星号。测试里这条配了负向控制：
  `process_rate_limit_exceeded` 这种不含数字的尾段必须**不被**打码，否则「打码」等于「抹平一切」。
- **切网关不敲网关**。`PUT /api/ai-sources/{id}/active` 只改文件里的 `active_id` 和内存缓存，
  一次请求都不发。这一条是有意和老工程分开的：那边切源顺手发一次探活，等于替用户花一次 token。
- **一处生效，三条链路共用**。整榜（`/api/ai/analysis`）、单股直评（`/api/ai/single/{code}`）、
  每日固定榜（`/api/ai/daily-rank/rank`）都从 `ai_gateway.ai_config()` 拿配置，
  而 `ai_config()` 先问 `active_source()`。所以「页面上切了源、只有某一条链路还在打老地址」这种事
  没有第二条路可走。没配齐时三条路都回同一句 `not_configured_message()`，说清缺的是哪几项、
  以及「这一次一分钱都没花」。

`GET /api/settings` 因此回两套字段：`ai_base_url` / `ai_model` / `ai_api_key_set` 是表单那三格现在的值，
`ai_active_*` 是下一次请求真正用的那一家。两者不一致时页面上必须都画出来——设置页上面四行读 `ai_active_*`，
下面三格是表单，网关表格里内置那行的名称就是「设置页里那三项」。

## 每日对账与模型记忆 JSON 快照

`services/ai_memory_service.py` 只做两件事：**把库里已有的东西渲染成一天两个文件**，
以及**跑一遍已有判据的对账**。它自己不产生任何判据、不敲任何网关。

- 对账判据仍是 `ai_picks` 那一份（判据日 = 推荐日之后库里第一个交易日、`pending` 不进分母、
  没什么可判时命中率是 `null`），这里不重写第二把尺子。
- 落盘的日子是**两个**：当天，加上上一个交易日。只导当天会让上一天的 `next_day` 永远是空的——
  样本配不成对，文件攒再多也攒不出「说过什么、后来对不对」。
- 「只渲染，不造数据」是可验证的：本库从来没落过 `涨停时间 / 封板次数 / 打开次数` 这三列，
  所以文件里**根本没有这三个键**，不是值为 0。这条由 `field_gaps` 跟着接口一起回，
  并且缺口清单里凡是「这一列有没有真的落过」的判断都走 `_null_counts` 现读库，不是抄一段固定文案。
- **没做成是 `null`，不是空对象**。`run_memory` 的回包先给 `reconcile` / `export_today` /
  `export_previous` 留 `None`：页面读到 `{}` 会当成「跑了，0 条」，读到 `null` 才知道那一步根本没成。
  某一条腿抛异常只进 `errors`，另外两条腿的结果照常带回——半截状态比整体失败有信息量，
  所以 `POST /api/ai-memory/run` 不把它包成 500（`…/export` 只有导出这一条腿，失败了才包）。
- 多点一次只是把同一天那两份重写一遍（临时文件 + `os.replace`），不会多出一份样本，也不会碰别的日子。
- 每日流水线里挂钩的是同一个函数（`sync_service._memory_pass`），所以「手动点一次」和「等自动那一拍」
  不是两套代码。

## 时间只有一个挂钟

全项目只认东八区（`core/clock.py` 里 `SHANGHAI = UTC+8`，不读系统时区）。写进**不带时区**的列
（`created_at`、`updated_at` 那一类）时统一用 `clock.now_naive()`，存的是东八区墙上时间。
存 UTC 的话，回测列表那一批「生成于」一律早八小时，而数据管理页那格「上次质量检查」跟它差一天——
两种挂钟住在同一张表里，页面就只能挑一个错。带偏移的字段（`last_quality_at`、`last_cleanup_at`）
是设置里的 ISO 字符串，末尾带 `+08:00`，跟库里的裸时间是两套读法，前端按字符串原样显示。

## 这批接口的页面接线状态

`/api/data/*` 落在「数据管理」页（`/data`）：概况卡、四个重建按钮、2 秒一拍的进度、每日覆盖表、质量闸门账、跳过账。
`/api/patterns/overview` 与 `/api/patterns/{code}` 落在「涨停形态样本」页（`/patterns`）。
`/api/matches/{code}`（读库与重算）、`/api/matches/compare`、`/api/matches/export` 落在「个股相似度」页——
那一页下面多了一块「库里落库明细」，选中左侧样本时右侧多一块「逐日对比」。
`/api/clusters/overview` 与 `/api/clusters/{code}` 落在「形态聚类」页（`/clusters`）：统计卡、组列表、组画像与成员、
散点图、分组数评估、目标股归属。页面上的分组数下拉与 `k` 是一件事，改下拉就地重算，两处（分组表与归属表）吃同一个 `k`。

`/api/ai-sources` 那五条落在「设置」页（`/settings`）的「大模型网关」这张表：计数、加一条、改一条、删一条、
设为当前都在同一页就地重算，`submitGateway` 每次改完都 `await loadSettings()`，所以上面「AI 总结」那四行
（生效网关 / 生效地址 / 生效模型 / 模型解读）跟着清单一起变，不需要重进页面。
表里正在生效那行的「删除」按钮是灰的，绕过按钮直接 `DELETE` 也回 422——两道闸各挡各的，都在测试里。

`/api/ai-memory/*` 那四条落在「每日 AI 固定榜」页（`/ai-rank`）的「每日 JSON 快照」这块：汇总读数、
按日文件表、两个目录路径与库里最新行情日；「跑一遍对账与快照」把 `POST /api/ai-memory/run` 的回包
（本轮定判 / 落某天 / 回填上一交易日三条读数与 `errors`）直接并进 store，不再回头 refetch 一遍——
谁跑出这些数，谁把它们交出去。`onVisibility()` 那一拍也读 `loadAiMemory()`，切回标签页时读数是新的。

两条还没有消费方：`GET /api/patterns/top`（接口通、有测试，页面上首屏读的是 `overview`）与
`GET /api/matches/summary`（某一天全市场汇总的分页）。后者是股票详情页与列表页要用的那一份，排在路线图的档 6。
