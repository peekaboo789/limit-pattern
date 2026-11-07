# 每日 AI 固定榜「前 100 行」是怎么挑出来的（旧版导出文档，按原文整理）

> 原文 2026-10-31 导出自旧项目（`文件:行号` 指向旧项目源码，本项目没有这些文件）。本文件保留原文的公式、常量、判据与数字；
> 旧项目特有的定时 / 快刷链路细节（`snapshot_for`、`live_payload`、120 秒全市场轮）只摘要。本项目实现与差异见 [README.md](README.md)。

一句话版：

```
每只股票的形态分 = 它「最近三天 K 线（上午时最后一天是今天的半天线）」的 67 维向量，
                  与「截至基准日的 5 个交易日里全部涨停样本（去掉它自己）」逐一比相似度，取最大值
前 100 行       = 全市场（含已涨停）按 (最高相似度降序, 最高相似度降序, 代码升序) 取前 100，
                  再按「已涨停在前、未涨停在后」两段拼起来，各段内仍按最高相似度降序
```

之后再让模型从这 100 行里挑 20 名（`DAILY_PICK_COUNT = 20`），那一步的打分公式见 [ai_score_formula.md](ai_score_formula.md)。

---

## 一、挑选链路

- 口径常量：`DAILY_FEED_ROWS = 100`、`DAILY_METRIC = "max"`（最高相似度）。
- 成员：上一轮全市场打分结果按口径取前 100 个代码；分数：只为这 100 个代码当场再取一次价、再打一次分；停牌（数据源没返回）当轮掉出，可能不足 100 行；重打分后不再从全市场补位。

### 排序键

```python
sorted(rows, key=lambda row: (-value(row), -row.max_similarity, row.stock_code))[:top_n]
```

固定榜 `metric = "max"`，键实际是 `(−最高相似度, −最高相似度, 股票代码升序)` → 取前 100。
同分时按代码字符串升序决定谁进第 100 名（判据 `sel_rank_key_tiebreak_by_code`：600001=97、000002=95、600003=95、300009=90 取前 3，得 600001、000002、600003）。

### 两段拆开再拼

- **已涨停的股票不剔除**，和未涨停的一起按分数竞争前 100。
- 喂给模型的顺序是「已涨停段在前、未涨停段在后」，**不是全局按分数排**（判据 `negative_feed_not_globally_sorted`：前 4 行是 70 / 69 / 68 / 99）。
- 「已涨停」是盘中现价判的，可能开板，以取价那一刻为准。
- 没有最高相似度的行跳过；剩下的按上面的顺序截前 100 行，`feed_rank` 是截断后的 1~100。

---

## 二、形态窗口：比的是哪三天

| 时段 | 判据 | 窗口最后一天 |
|---|---|---|
| 盘前 `pre_open` | 早于 9:30 | 三天都是严格早于今天的已收盘日 |
| 上午 `morning` | 9:30 ≤ 时刻 < 11:30 | 用此刻报价现造的当天半天线（`is_partial=True`，量额为截至此刻的累计值） |
| 之后 `frozen` | ≥ 11:30 | 只读上午留档的那份；没有留档就退回「三天都是已收盘日」 |

**整榜统一一把尺**：这一轮只要有一只造出了半天线，整榜都按「含今天上午」算；造不出的那只当轮跳过，不混用两种窗口。库里已有今天全天线时先丢掉，只用半天线。

### 三道闸

| 闸 | 跳过原因 | 条件 |
|---|---|---|
| 1 | `insufficient_trading_days` | 可用交易日 < 3 |
| 2 | `listed_less_than_20_days` | **完整**交易日 < `MIN_LISTED_TRADING_DAYS = 20`；半天线不占名额 |
| 3 | `invalid_bar_in_window` | 三天里有一天缺行情 / 停牌 / 零成交 |

窗口三天的标签是 `("T-3", "T-2", "T-1")`。上午这条路上「T-1」就是今天那根半天线。历史行情按 `trade_date <= as_of_date` 截断。

---

## 三、样本池：和谁比

- **基准日** = 严格早于今天的最近一个有涨停样本的日子。
- **样本池** = 以基准日收尾的最近 `SAMPLE_POOL_TRADING_DAYS = 5` 个交易日的全部涨停样本，按涨停日、代码升序。每个样本的向量只含它**涨停日之前**的三天，涨停当天不进向量。
- 标准化器和距离尺度都在样本池上现拟合（按基准日缓存）。
- **去掉自己**：`keep = [i for i, sample in enumerate(samples) if sample.stock_code != code]`——同一代码的**所有**样本都去掉（判据 `sel_score_excludes_own_code`：含自己时最高分 100，去掉后 12.5）。

---

## 四、每一行的四个数

```python
"sample_count": int(row_scores.size),
"max_similarity": round(float(row_scores[best]), 2),
"avg_similarity": round(float(row_scores.mean()), 2),
"high_similarity_count": int((row_scores >= thresholds[0]).sum()),
"band": describe_band(float(row_scores[best]), thresholds),
"top_sample": samples[keep[best]].stock_code,
```

| 列 | 公式 |
|---|---|
| 比对样本数 | `len(keep)`（池子 − 自己的样本） |
| **最高相似度**（选榜就用它） | `round(max_j score(i, j), 2)` |
| 平均相似度 | `round(mean_j score(i, j), 2)` |
| 高度相似数量 | `#{ j : score(i, j) ≥ 90 }`（`similarity_threshold_high = 90.0`） |
| 档位 | ≥90 高度 / ≥80 较高 / ≥70 一般 / ≥60 低 / 其余弱（60 是写死的） |
| 最像的样本 | argmax 那一个样本的代码 |

---

## 五、相似度 `score(i, j)` 的完整公式

### 1. 67 维向量

每天 20 个特征 × 3 天 = 60 维，加 7 个三日汇总 = 67 维：

| 组 | 每天的字段 | 维数 | 组权重 | 组内字段权重 |
|---|---|---|---|---|
| price_trend 价格走势 | open/high/low/close_change | 12 | 0.35 | close 0.40、high 0.25、low 0.20、open 0.15 |
| kline_structure K 线结构 | amplitude、body_length、upper_shadow、lower_shadow | 12 | 0.20 | 各 0.25 |
| volume 成交量 | volume_change、volume_ratio_5、volume_ratio_10 | 9 | 0.15 | 各 1/3 |
| amount 成交额 | amount_change、amount_ratio_5、amount_ratio_10 | 9 | 0.10 | 各 1/3 |
| turnover 换手 | turnover_rate、turnover_change | 6 | 0.10 | 各 0.5 |
| position 历史位置 | position_5/10/20/60 | 12 | 0.05 | 各 0.25 |
| three_day_trend 三日趋势 | three_day_return、volume_growth、amount_growth、turnover_change、close_trend、volume_trend、amount_trend | 7 | 0.05 | 各 1/7 |

`three_day_amplitude` 只做展示，不进向量。

逐维权重 `w_k` = 组权重 × 组内字段权重，先按组归一（让每组合计恰好等于组权重）、再整体归一。实测七组合计 0.35 / 0.20 / 0.15 / 0.10 / 0.10 / 0.05 / 0.05，总和 1.0；例如每天 close_change 一维 = 0.35 × 0.40 / 3 = 0.04667（判据 `close_change_each_day`）。

### 2. 标准化 + 加权

```python
center = np.median(matrix, axis=0)
q75, q25 = np.percentile(matrix, [75, 25], axis=0)
scale = q75 - q25
scale = np.where(scale <= _EPS, 1.0, scale)
```

```
z_k = (x_k − 中位数_k) / IQR_k × √w_k        （中位数、IQR 都在样本池上算；IQR ≤ 1e-9 的列按 1）
```

### 3. 距离分 + 余弦分 + 合成

```
d          = ‖z_i − z_j‖₂
scale      = 样本池内两两距离的中位数
欧氏分     = 100 · exp(−(d / scale)²)
余弦分     = (cos(z_i, z_j) + 1) / 2 · 100         （任一向量范数 ≤ 1e-9 时 cos 记 0）
score(i,j) = round( clip(0.8·欧氏分 + 0.2·余弦分, 0, 100), 2 )
```

权重 0.8 / 0.2 来自配置（`euclidean_similarity_weight` / `cosine_similarity_weight`）；`SimilarityEngine.fit` 签名里的默认值 0.7 / 0.3 在这条路上不生效（判据 `engine_weights_from_settings`）。

欧氏分衰减表（实测，`decay_table`）：d/scale = 0 → 100、0.1 → 99.0、0.3 → 91.4、0.5 → 77.9、1.0 → 36.8。

### 4. 这对「前 100」意味着什么

余弦分最多贡献 0.2 × 100 = 20 分，所以总分 ≥ 90 必须 0.8 · 欧氏分 ≥ 70，即欧氏分 ≥ 87.5，即

```
d ≤ √(−ln 0.875) · scale ≈ 0.3654 · scale
```

（判据 `sel_90_needs_euclid_87_5` 实测 0.3654。）10-08 那份 100 行的最高相似度在 94.87 ~ 97.1 之间。所以前 100 名实际上是由「离池里最近的那个涨停样本有多近」决定的。

---

## 六、真实输入对账（旧项目开发库 10-08，只读）

- 顺序与元数据：通过（已涨停 1 行在前、未涨停 99 行在后，最高相似度 97.1 → 94.87）。
- 只算 100 只 vs 全市场一轮：通过 100/100。
- 去掉半天线做负向对照：100 行全变。
- 逐行四个数完全复现：没过（71/100 一致，最高相似度最大偏差 0.76）。
- 前 100 成员：没过（97/100 一致，换了 3 只，都在第 100 名附近 94.84 ~ 95.16）。偏差原因没查实。

---

## 七、判据清单（原文 52/54）

- 形态公式 30 条全过：67 维与七组维数、组权重合计、close_change 每维权重；衰减表、scale=0 退化；引擎权重取自 settings、自比 = 100、典型距离处欧氏分 36.79、批量 = 单对比、手工重建欧氏分 / 余弦分；scale = 池内两两距离中位数、中心 = 中位数、IQR=0 退化为 1、缺失填 0 不等于中位数；五档、汇总比率；特征、趋势编码、向量长度。
- 负向对照：组权重抄错会被抓到；高斯核换成 exp(−d/scale) 后 0.5·scale 处 77.88 → 60.65，判据会红。
- 挑选链路 16 条全过：同分按代码升序、拆段后段内顺序不变、去掉自己的样本、窗口、已涨停在前、截 100、0.3654。

---

## 八、原文的边界说明

1. **已涨停的股票在前 100 里**，而且排在喂数的最前面；它们盘中可能开板。
2. **半天线的量能是半日累计**，样本里全是全天线，所以上午时成交量 / 成交额 / 换手那几组系统性偏低；这一榜的分数不能和不含今天的榜比大小。
3. **成员与分数不是同一刻**（旧项目链路特有）。
4. **第 100 名同分时按代码字符串升序**决定去留，和形态无关。
5. **缺失特征填 0 是在标准化之前填的**：标准化后是 `(0 − 中位数) / IQR`，只有那一列中位数恰好为 0 时才「不表达偏离」，与 docstring「标准化之后 0 即中位数」不符（判据 `zero_fill_is_not_median`：中位数 12、IQR 2 时，缺失值标准化后是 −6）。**本项目 v0.4.0 按 docstring 本意修正为标准化后补 0（= 中位数）**。
6. `SimilarityEngine.fit` 签名默认 0.7 / 0.3，生效的是配置里的 0.8 / 0.2；衰减表只描述欧氏分，不是总分。
