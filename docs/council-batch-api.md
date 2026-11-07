# Council Batch API（阶段 1）

仅覆盖后端 TXT 解析、批次持久化与串行队列，不含前端界面约束。

基址：`/api/council`

## 1. `POST /batch/preview`

请求体：**原始 TXT 文件字节流**，必须是 `UTF-8` 或 `UTF-8 BOM`。
可选请求头：`x-file-name: xxx.txt`

限制：
- 最大 `128 KiB`
- 单行最大 `4096` 字符
- 总行数最大 `1000`
- 候选上限 `100`
- 仅按表头中的 `代码` / `名称` 列取值
- 不从 `最相似样本` 等其它列提取代码

成功返回：
- `preview_version`
- `source_name`
- `source_date`：来自元数据 `数据日期`
- `default_eval_date`：默认等于 `source_date`
- `items[]`：有效候选，字段含 `line/code/name/market/board/board_label/selected`
- `errors[]`：无效或不支持项，字段含 `line/code?/name?/reason`
- `counts { valid, invalid, total }`

失败：
- `422 detail="只支持 UTF-8 / UTF-8 BOM 文本"`
- `422 detail="未找到数据日期"`
- `422 detail="未找到包含代码/名称的表头"`
- `422 detail="文件超过 131072 字节上限"`
- `422 detail="第 N 行超过 4096 字符上限"`

## 2. `POST /batch/start`

JSON：

```json
{
  "preview": {"source_date": "2026-11-06", "items": [...]},
  "eval_date": "2026-11-06",
  "profile_ids": ["p1", "p2"],
  "source_name": "ranking.txt",
  "idempotency_key": "optional-client-key"
}
```

语义：
- `profile_ids` 必须全部是已配置且有密钥的模型
- 至少两个模型
- `eval_date` 不能晚于今天
- 未提供 `idempotency_key` 时，后端基于 `source_date + eval_date + profile_ids + codes` 生成
- 相同幂等键重复请求时，返回既有批次，不重复创建

成功返回完整批次对象。

## 3. `GET /batch?limit=50`

返回：`{ "items": [batch...] }`

## 4. `GET /batch/{batch_id}`

返回单个批次：
- `id/source_name/source_date/eval_date`
- `status`：`pending | running | done | interrupted`
- `stage`
- `profile_ids`
- `item_count/done_count/failed_count/cancelled_count`
- `parser_summary`
- `error`
- `created_at/updated_at/started_at/finished_at`
- `items[]`

`items[]` 字段：
- `id/batch_id/position`
- `stock_code/stock_name/source_market`
- `status`：`pending | running | done | failed | cancelled | interrupted`
- `stage`
- `session_id`：若已实际调用单股 council，则指向既有 `AiCouncilSession`
- `probability_percent`：仅来自主持模型结论规范化结果；**绝不继承导入 TXT 的预测分**
- `confidence`
- `summary`
- `error`
- `details`
- `started_at/finished_at`

## 5. `POST /batch/{batch_id}/cancel`

语义：
- 只取消 `pending` 项
- 不停止已开始的单股讨论
- 不删除历史 `AiCouncilSession`

返回更新后的批次对象，另附：
- `cancelled_now`

## 6. `POST /batch/{batch_id}/retry-failed`

语义：
- 仅把 `failed` 项重置为 `pending`
- `done` 项不得重跑
- 已关联的失败 `session_id` 断开，等待重新排队

返回更新后的批次对象，另附：
- `retried`

## 7. 队列与互斥语义

- 批次 worker 为**单线程串行**
- 每股实际执行复用既有单股 council `start_sync()`
- 因此天然受现有 `_START_LOCK` 与单会话规则约束
- 不会 20 并发，不绕过单股锁

## 8. 重启恢复语义

启动时：
- 遗留 `running` 批次标记为 `interrupted`
- 其中 `running` item 标记为 `interrupted`
- `pending` item 保留 `pending`
- **不会自动重新调用付费模型**
- 需用户随后显式调用失败重试或重新开始逻辑

## 9. 历史删除兼容

若某 item 的 `session_id` 指向的单股历史后来被删除：
- 本阶段仅保留历史 `session_id` 引用
- 前端/原生端应在打开会话时处理 404 并显示“已删除/不可打开”
- 不复活被删记录

## 10. 已知局限（阶段 1）

- 仅提供后端 API，不含 Vue/SwiftUI 页面
- preview 端点当前直接接收原始文本字节流，不是 multipart
- `interrupted` 后的“继续未完成项”尚未单独提供专门 API；当前通过 `retry-failed` 处理失败项
