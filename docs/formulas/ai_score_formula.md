# AI 打分的计算公式（从源码逐字导出）

导出时间：2026-10-31。文中每一个数字、每一段中文口径都是从下列四个文件里抄出来的，
不是重新描述的；`file:line` 指向源码里那一行的实际位置，改代码那天这份文档就该跟着改。

权威来源共四处：

| 公式 | 文件 | 关键行 |
| --- | --- | --- |
| 综合评分（AI 打分的主公式） | `backend/app/services/score_model.py` | 51–121、448–546、266–360 |
| 单股历史总分（对账页那一栏） | `backend/app/services/ai_pick_stats.py` | 50、79、91 |
| 预测分（三证据加权） | `backend/app/services/prediction_service.py` | 60–74、220–227、470–497 |
| 档位历史发生率（回测统计口径） | `backend/app/services/backtest_service.py` | 48–77、106 |

一句话总纲：**分数全部由后端按固定权重算出来，大模型只是解读器**。模型自报的那一个数
（`model_score`）只用来显示「它给的与公式差多少」，不当结论用。

---

## 一、综合评分（score_model.py，用户 2026-10-08 定稿）

用户给的原文规则（**逐字照做，不自行加码**）：

> 综合评分规则：形态权重 35% / 技术权重 20% / 量能权重 15% / 资金权重 15% / 历史成功率 15%
> 风险扣分：risk_score >=80 扣20分，60-80 扣10分，40-60 扣5分
> 等级：90-100 强关注 / 80-89 高关注 / 70-79 观察 / 60-69 一般 / 60以下 低匹配
> 重要规则：不重新计算指标、不修改输入数据、不加入新闻公告基本面消息、不预测确定涨停

### 1. 主式

```
加权分 weighted = Σ(权重ᵢ × 指标ᵢ) / Σ(有项的权重ᵢ)
综合评分 composite = round( clip( weighted − 风险扣分, 0, 100 ), 2 )
各项贡献 contributionsᵢ = round( 权重ᵢ × 指标ᵢ / Σ(有项的权重ᵢ), 2 )
模型差值 model_gap = round( model_score − composite, 2 )      # 正数 = 模型比公式给得高
```

实现：`score_of()`（`score_model.py:471`）、`risk_penalty_of()`（`:448`）、`level_of()`（`:459`）。
`ScoreBreakdown.weighted_score` 与 `contributions` 各项之和是同一个数——有用例钉着这条等式。

### 2. 五项权重（`score_model.py:51-55`，加起来必须等于 1，有用例钉）

| 项 | 键 | 权重 | 读的是哪个输入指标（`INDICATOR_OF_ITEM`，`:98`） |
| --- | --- | --- | --- |
| 形态 | `pattern` | 0.35 | `similarity_score`（三日形态相似度） |
| 技术 | `technical` | 0.20 | `technical_score` |
| 量能 | `volume` | 0.15 | `volume_score` |
| 资金 | `capital` | 0.15 | `capital_score`（**恒为 None**，见下） |
| 历史成功率 | `history` | 0.15 | `history_limit_up_rate` |

### 3. 风险扣分档（`RISK_TIERS`，`score_model.py:111-112`）

```
(80.0, 20.0)   风险分 ≥ 80 扣 20
(60.0, 10.0)   60 ≤ 风险分 < 80 扣 10
(40.0,  5.0)   40 ≤ 风险分 < 60 扣  5
风险分 < 40 不扣；风险分为 None 不扣，并把 risk_score 列进 missing_items
```

用户写的是「60-80」「40-60」，两个区间在 60 和 80 上重叠；源码取**下界含、上界不含**
（80 归「扣 20」，60 归「扣 10」，40 归「扣 5」）——这是唯一不自相矛盾的读法。

### 4. 等级档（`LEVEL_TIERS`，`score_model.py:115-121`）

```
composite ≥ 90 → 强关注      ≥ 80 → 高关注      ≥ 70 → 观察      ≥ 60 → 一般      否则 → 低匹配
```

同样「下界含」：90 分是强关注，89.9 分是高关注。`composite` 为 None 时 `level_of()` 回空串，
页面显示「—」而不是「低匹配」。

### 5. 三条不许折叠成 0 的规则

1. **缺项的权重摊给其余项**：分母是「有项的权重和」`weight_total`，不是 1。
   资金项天天缺，所以其余四项天天按 `0.35/0.85`、`0.20/0.85`、`0.15/0.85`、`0.15/0.85` 归一。
   不摊的话就是拿「我没数据」当「它不好」。
2. **`capital_score` 恒为 None**（`:231` 的 `indicators_of()` 里那一行写死）：
   默认数据源（腾讯）日线里 `amount` / `turnover_rate` 整列为空，`daily_features` 的
   `volume` / `amount` / `turnover_rate` / `amount_ratio_5` 同样是 0 行有数
   （2026-10-08 在开发库 2026-09-30 数过：`position_20`、`volume_ratio_5` 等 5146 行全有数，
   `turnover_rate`、`volume`、`amount` 0 行有数）。没有原料就写 None，不写 0。
3. **`pattern_match_rate`（近 5 日涨停形态匹配率）不占权重**：用户权重表里没有它的位。
   它照样进输入、页面上看得见，但不参加上面那个乘法；要给它权重必须由用户重拍一次口径。
   ```
   pattern_match_rate = high_sim_count / sample_count × 100      # 分母为 0 或任一端缺 → None
   ```

`has_signal(breakdown)` ⇔ `weight_total > 0`（`:514`）：五个加权项全没数时，`composite_score`
是 0.0 但它是「无从谈起」不是分数，`read_composite()` / `read_level()` / `read_gap()`（`:523` 起）
一律回 None / 空串 / None，页面画「—」。

### 6. 七个输入指标（`INDICATOR_ORDER`，`score_model.py:87`；中文名在 `INDICATOR_LABELS:77`）

`similarity_score` 三日形态相似度 · `pattern_match_rate` 近5日涨停形态匹配率 ·
`technical_score` 技术形态评分 · `volume_score` 成交量评分 · `capital_score` 资金评分 ·
`history_limit_up_rate` 历史相似样本成功率 · `risk_score` 风险评分（走扣分，不走加权）

原料列 `FEATURE_COLUMNS`（`score_model.py:565`）：
`position_10, position_20, position_60, body_percent, close_change, volume_ratio_5, volume_ratio_10, amplitude`。
读的是 `daily_features` 里**最近一个已收盘基准日**那一行——所以技术/量能/风险三项是昨天收盘
的结构，不是盘中实时重算，页面上必须把这一句说清。

单位口径：只有 `position_*` 是 0~1 比率（要 ×100）；`amplitude`、`close_change`、`body_percent`
落库前已是百分数；`volume_ratio_*` 是倍数。子项在加权前各自夹进 0~100（`_clamped`），
None 保持 None。

---

## 二、三个分项公式

### 1. 技术形态评分（`technical_score_of`，`score_model.py:266`）

```
technical_score = [ 0.45 × mean(position_10, position_20, position_60) × 100
                  + 0.30 × body_percent
                  + 0.25 × (50 + close_change × 10) ] / Σ(有项权重)
```

- 区间位置：站上高位 = 趋势结构在走强（库里那 29 个 MA/MACD/RSI 列因长期全 NULL 已被物理删掉）。
- K 线实体：只取「饱满度」这一个方向，不去猜影线的含义。
- 当日动能：平盘 50 分，每 1 个百分点加减 10 分。`close_change` 实测最小到 −72.5（除权那天），
  不夹就会让一根除权柱把整只股票的技术分拖成负数。

### 2. 成交量评分（`volume_score_of`，`score_model.py:306`）

```
volume_score = 0.60 × _volume_points(volume_ratio_5) + 0.40 × _volume_points(volume_ratio_10)
               （同样除以有项权重和；两个量比都缺 → None）

_volume_points(x):   x < 0 → 0
                     0 ≤ x < 1 → 30 + 30x
                     1 ≤ x < 2 → 60 + 25(x−1)
                     2 ≤ x < 3 → 85 + 10(x−2)
                     x ≥ 3     → 95（封顶不给 100）
```

实测对照：量比 0 → 30 分；0.7 → 51；1.0 → 60；1.5 → 72.5；2.0 → 85；3.0 及以上 → 95。
**温和放量最好，缩量最差，爆量不给满分**——量比 3 倍以上既有「启动」也有「出货」，
扣分那条记在 `risk_score` 那本账里，不在量能这里记两次。

### 3. 风险评分（`risk_score_of`，`score_model.py:350；越高越危险`）

```
risk_score = [ 0.35 × position_20 × 100                 # 高位
             + 0.25 × max(0, 当日涨幅%) × 10            # 追高（跌与平盘记 0）
             + 0.20 × amplitude% × 5                    # 振幅（20% 记满 100）
             + 0.20 × max(0, volume_ratio_5 − 3) × 25 ] # 爆量（3 倍以下 0 分）
             / Σ(有项权重)；四项全缺 → None，此时不扣分
```

「追高」用的是榜上的**实时涨幅**而不是 `close_change`，因为「今天追高了多少」必须是今天的事。
跌幅记的是 **0 分而不是缺项**——这个 0 照样进分母、把其余子项摊薄（实测见第七节第 1 条）。
库里从来没有「风险分」这个概念（2026-10-08 查过：全库无该列、无该词），这一项是本次新加的合成。

---

## 三、单股历史总分（ai_pick_stats.py）

```
HISTORY_FORMULA = avg_score × (0.3 + 0.7 × (h+1)/(settled+2))        # :50
总分 = max(0, min(100, int(round(raw))))                              # score_from_history, :91
命中率 hit_rate = round(hit × 100 / (hit + miss), 2)                 # :79
```

- `(h+1)/(settled+2)` 是 **Laplace 平滑**：1/1 不读成 100%、0/1 不读成 0%；样本少的时候整头被
  拉回「自评 × 0.3」附近（相当于没对上过账时先只信它自评的 3 成）。
- `settled` 只含 `hit + miss`，**`pending` 永远不进分母**。
- `avg_score is None` → None（页面「—」）；`settled == 0` → 直接整数化的自评，
  并带 `ALL_PENDING_FORMULA = "avg_score（samples all pending — no hit/miss yet）"` 标签说明白。
- 一条定判都没有时 `hit_rate` 回 None 而不是 0.0%：「还没有样本」和「一次都没中」是两件事。
- 重命中率、轻自评：这一栏的尺子是**它对上账没有**，不是它自己说几分。

数据源是 `ai_pick_results` 表，纯读，不花网关钱。同一份汇总既喂页面的那几个数，
也喂提示词里的 `ai_history` 那几个键——「用户看到的它」和「模型看到的它」是同一个账。

---

## 四、预测分（prediction_service.py，盘中 AI 综合分析沿用同一套）

```
PREDICTION_FORMULA:
预测分 = 0.5 × 前日最高匹配度
       + 0.3 × 相似度加权连板率
       + 0.2 × 档位历史发生率相对基准倍数（按 2 倍封顶）
缺失项的权重摊给其余项；观测数不足 50 条的档位不参与打分。

band_term = round( min(band_lift, 2.0) / 2.0 × 100, 2 )        # band_lift 为 None → 该项 None
预测分   = Σ(权重ᵢ × 项ᵢ) / Σ(有项权重ᵢ)                       # composite_score, :477
```

常量：`WEIGHT_SHAPE = 0.5`、`WEIGHT_CARRY = 0.3`、`WEIGHT_BAND = 0.2`、`LIFT_CAP = 2.0`
（`:69-71`、`:67`）。三项全缺不会出现（shape 恒有值），全缺时分数就等于形态分本身。

- **相似度加权连板率**（`CarryEvidence.weighted_rate_percent`，`:470`）：
  `round(hit_weight / weight × 100, 2)`，权重就是相似度本身；`weight ≤ 0` → None，不假装是 0。
  取证据上限 `EVIDENCE_MATCH_LIMIT = 20`（`:60`）。
- **档位倍率**（`:220`）：`band_lift = round(band_rate / run.baseline_rate, 4)`，
  只有该档 `sample_count ≥ MIN_BAND_OBSERVATIONS`（50 条，`backtest_service.py:106`）才给值。

### 档位怎么切（backtest_service.py）

```
BAND_FLOORS = (90.0, 80.0, 70.0, 60.0)                         # :48
band_of(score) → "90~100" / "80~90" / "70~80" / "60~70" / "<60" # :57
```

分档下沿也可以跟随设置页阈值（`band_floors_from_settings()`，`:67`：
`similarity_threshold_high / second / third` 再加 `60.0`），不是写死的一套。

每档统计的是 `BucketStat`：样本数 / 次日涨停次数 / 实际发生率，上沿恒为 100。

> 本模块检验的是「形态相似度」与「下一交易日涨停」的历史统计关联性，
> 输出的历史实际发生率不是预测保证，不构成投资建议。（`BACKTEST_DISCLAIMER`，`:51`）

---

## 五、给模型的提示词里那一份（`PROMPT_RUBRIC`，score_model.py:173）

页面上的 `FORMULA_TEXT`（`:125`）与喂给模型的 `PROMPT_RUBRIC`（`:173`）**都是从上面的常量拼出来的，
不另抄一遍数字**——手抄的口径文本会在改权重那天变成假话。提示词对模型说的话，摘要三条：

1. 「评分模型由本系统给定，你不是打分器而是解读器」；这些数你已经有了，**不要重新计算，也不要修改**。
2. 你写的 `score` 是你自己的相对判断，页面以本系统的综合评分为准，并把两者的差一起显示；
   `reason` 要写成模型依据：在给定权重下你更信哪几项、哪一项的读数让你要偏离这个等级，不要复述字段清单。
3. `capital_score` 现在是 null：null 不是 0 分。`pattern_match_rate` 不在加权项里，可以拿它说事，但不要拿它改分。

---

## 六、落库与展示口径（同一套数，三处入口）

三条链路（盘中整榜 / 每日固定榜 / 单股直评）共用 `score_model.py` 这一份实现，权重与档位不留两处各写一次。
落库与进提示词存的是 `ScoreBreakdown.as_dict()` 那一份账，**再由调用方补上 `has_signal` 这个键**
（`ai_analysis_service.py:595`）：`composite_score` / `level` / `contributions` / `risk_penalty` /
`missing_items` / `weight_total` / `indicators` / `model_score` / `model_gap` / `has_signal`。
这里有个坑要说明白：`as_dict()` 本身**不含** `has_signal`，拿裸的 `as_dict()` 去喂 `read_composite()`
会读出 0.0 而不是 None——「五项全无数」就被印成「综合评分 0 分」。单测
`tests/test_score_model.py:843` 钉的就是「挂上去那一份里必须有这个键」。
2026-10-08 之前的旧存档压根没有这份字典，`read_composite()` 回 None，页面显示「—」而不是 0 分。

页面六栏「综合评分 / 等级 / 形态贡献 / 技术贡献 / 量能贡献 / 资金贡献 / 风险影响」读的就是这里的字段，
**模型不参与生成它们**。

---

## 七、把公式代进真实实现跑了一遍（2026-10-31，35 条判据全过）

下面是拿 `backend/.venv/Scripts/python.exe` 直接 import 这四个模块、按本文的式子算一遍再与模块返回值
对账的实测输出（只读计算，不连库、不调网关）。**每一项都相符**；另配了两条「故意把数写错」的
负向控制（把形态权重 0.35 改成 0.30、把量比封顶 95 当成 100），两条都判红，说明这台量具抓得住错。

| 算的东西 | 输入 | 本文式子算出 | 模块返回 |
| --- | --- | --- | --- |
| 综合评分（资金有数的假设情形） | 形 92 / 技 70 / 量 60 / 资 50 / 史 30，风险 85 | 67.2 − 20 = 47.2 | 47.2，等级「低匹配」 |
| 综合评分（资金缺项，天天如此） | 形 92 / 技 70 / 量 60 / 史 30，风险 55 | (0.35×92+0.20×70+0.15×60+0.15×30)/0.85 − 5 = 65.24 | 65.24，等级「一般」，`weight_total` 0.85 |
| 五项全无数 | 只有 `pattern_match_rate` 与风险分 | — | `composite` 0.0、`has_signal` False、`read_composite` None（带旗标时）/ 0.0（裸 `as_dict`） |
| 技术形态评分 | position 0.8/0.6/0.4、`body_percent` 50、`close_change` 3 | 62.0 | 62.0 |
| 成交量评分 | 量比 5 日 2.0、10 日 1.0 | 0.6×85 + 0.4×60 = 75.0 | 75.0 |
| 量比分段表 | 0 / 0.7 / 1 / 1.5 / 2 / 3 / 4 / −1 | 30 / 51 / 60 / 72.5 / 85 / 95 / 95 / 0 | 完全一致 |
| 风险评分 | `position_20` 0.9、振幅 12、量比 4、当日涨 6 | 63.5 | 63.5 |
| 风险扣分边界 | 80 / 79.99 / 60 / 59.99 / 40 / 39.99 / None | 20 / 10 / 10 / 5 / 5 / 0 / 0 | 完全一致（下界含） |
| 等级边界 | 90 / 89.99 / 60 / 59.99 / None | 强关注 / 高关注 / 一般 / 低匹配 / 空串 | 完全一致 |
| 单股历史总分 | `avg_score` 80、h 3、settled 5 | 80×(0.3+0.7×4/7) = 56 | 56；`avg_score` 77.6 且 settled 0 → 78；`avg_score` None → None |
| 命中率 | 3 中 7 失 / 0 中 0 失 | 30.0 / 没有样本 | 30.0 / None |
| 预测分（三项齐） | shape 92、carry 40、倍率 1.5 | 0.5×92+0.3×40+0.2×75 = 73.0 | 73.0 |
| 预测分（档位观测不足） | shape 92、carry 40、band 缺 | (46+12)/0.8 = 72.5 | 72.5，`terms.band` None |
| 预测分（倍率超上限） | shape 92、倍率 3.0（>2 封顶） | (0.5×92+0.2×100)/0.7 = 94.29 | 94.29，`terms.band` 100 |
| 相似度加权连板率 | (90 中, 40 中, 10 失) | 130/140×100 = 92.86 | 92.86；一个样本都没有 → None |
| 分档标签 | 95 / 90 / 89.9 / 80 / 79.9 / 70 / 69.9 / 60 / 59.9 | 90~100 / 90~100 / 80~90 / 80~90 / 70~80 / 70~80 / 60~70 / 60~70 / <60 | 完全一致 |

两处只有跑一遍才会露出来的细节，补在本文里：

1. **当日跌的时候，「追高」子项是 0 分而不是缺项**（`max(0, 涨幅) × 10` 给得出 0.0）。
   所以 `risk_score_of({"position_20": 0.5}, change_percent=-3)` = (0.35×50 + 0.25×0)/0.60 = **29.17**，
   而不是 17.5——那个 0 分照样进分母，把其余子项摊薄了。只有 `change_percent` 传 None 时这一项才算缺。
2. **`has_signal` 不在 `as_dict()` 里**（见第六节那一条），判「空」必须看补过旗标的那一份。

---

## 八、这份文档的边界

- 本文只写**已经在源码里实现**的公式；`capital_score` 为什么恒缺、缺多少行有数，抄的是
  `score_model.py` 模块开头 2026-10-08 实测的那几句，本轮没有重新数库。
- 权重与档位的**出处**是用户 2026-10-08 的定稿原文（第一节引了一段），落地时那些边界归属
  （60/80 重叠、下界含）是源码里定的读法，不是用户原话。
- 文件放在项目根，`.dockerignore` 的 `*.md` 规则会把它排除在构建上下文之外，也不会进任何 EXE；
  它是给人看的导出件，不是产物的一部分。

