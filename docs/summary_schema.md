# 每日总结 JSON

文件：数据目录下的 `summaries/YYYY-MM-DD.json`。同一份内容也在表 `ai_daily_summaries`。清理行情时不会删它们。

数字（家数、板块、连板、形态、量能）由本系统根据当天行情先算好。模型只写 `headline` 和 `key_points`，不能改这些数字，也不许用新闻、公告或财报。

```json
{
  "date": "2026-06-12",
  "limitup_count": 2,
  "sectors": [
    {"name": "主板", "count": 2}
  ],
  "ladder": [
    {"height": 1, "count": 2, "codes": ["600000", "000001"]}
  ],
  "patterns": [
    {"name": "放量上攻", "count": 1, "example_code": "600000"}
  ],
  "volume_features": {
    "median_turnover_rate": 1.5,
    "median_volume": 1200,
    "sample_count": 2
  },
  "key_points": [
    "当天两只涨停都在主板"
  ],
  "headline": "2026-06-12 共 2 只涨停",
  "model": "demo-model",
  "generated_at": "2026-06-12T17:20:00+08:00"
}
```

| 字段 | 含义 |
| --- | --- |
| `date` | 交易日 |
| `limitup_count` | 当天涨停只数 |
| `sectors` | 按板块计数。名称是主板、创业板、科创板。没有行业题材数据，所以不编题材 |
| `ladder` | 连板高度。`height` 从 1 起，`codes` 是股票代码 |
| `patterns` | 用成交量和前一天比出来的形态：放量上攻、缩量板、普通涨停 |
| `volume_features` | 当天涨停股的换手率中位数、成交量中位数 |
| `key_points` | 几句要点 |
| `headline` | 一行标题 |
| `model` | 写出标题的模型名。统计版是空字符串 |
| `source` | `statistics` 是系统自己的统计；`ai` 表示标题和要点来自模型 |
| `ai_error` | 模型没配或调用失败时的原因。成功时为 null |
| `generated_at` | 生成时间，东八区 |

列表接口 `GET /api/summaries` 只返回 `date`、`limitup_count`、`headline`。
