# 讨论数据源研究、维护与清单协议（v0.7.14）

研究时间：2026-11-07，北京时间（Asia/Shanghai）。只阅读公开文档/接口schema并独立编写httpx适配器，不安装下列SDK、不执行下载的代码。SDK的开源许可证不等于上游数据的商用授权或可用性保证。

## GitHub项目筛选

| 完整项目地址 | GitHub最近push（北京时间） | 许可证（GitHub识别） | 覆盖 / 免费边界 / 选择 |
|---|---|---|---|
| https://github.com/akfamily/akshare | 2026-11-06 23:44:59 | MIT | 维护活跃，多家上游行情/资金/财报/公告接口目录。优先用于查schema和来源归属；文档中有付费/认证/加密接口，不是全部免token。本次仅选免登录HTTPS公开部分，未照搬实现。 |
| https://github.com/Micro-sheep/efinance | 2026-07-17 15:47:42 | MIT | MIT；股票行情、资金等主要封装东方财富。适合作字段对照；不是独立于东财的第二份证据，不额外安装SDK。 |
| https://github.com/mootdx/mootdx | 2024-07-16 17:09:05 | MIT | 文档含同花顺00不复权/01前复权的编码，实测00明文JSONP。仅借鉴公开协议，不引入通达信TCP/SDK；较久未push，不把它称为当前活跃服务。 |
| https://github.com/shimencaiji/baostock | 2019-10-25 08:33:03 | 未识别，不复制代码 | 所查非官方镜像多年未push，API未识别许可证；不是证明没有许可证，也不冒称官方维护。BaoStock需独立SDK/服务协议适配，本次不加入依赖或声称已接入。 |
| https://github.com/waditu/tushare | 2024-03-13 22:31:20 | BSD-3-Clause | 旧开源客户端；Pro是注册、token/权限/积分体系（部分基础免费、有配额），不是默认免注册数据源，本次不接入、不索要token。 |

公开参考文件：
- https://github.com/akfamily/akshare/blob/main/akshare/stock/finance/stock_disclosure_cninfo.py （巨潮股票orgId映射与公告查询）
- https://github.com/akfamily/akshare/blob/main/akshare/stock/quote/stock_hist_tx.py （腾讯、不复权字段）
- https://github.com/akfamily/akshare/blob/main/akshare/stock/quote/stock_zh_a_sina.py （新浪渠道）
- https://github.com/Micro-sheep/efinance/blob/main/README.md
- https://github.com/mootdx/mootdx/blob/master/mootdx/contrib/adjust.py （仅接口参数事实，不拷贝实现）
- https://tushare.pro/document/1?doc_id=5 （Org与Pro定位、免费/积分边界；不把整个Pro误称全部收费）

## 产品已内置的真实上游（不是SDK名字）

28个已知解析任务、7个渠道组，默认启用27个任务（上交所当前网络未确认覆盖，默认关闭，可手动启用）。一个站点多个任务不算多家独立来源。

| 真实上游 | 覆盖 | 当前免费/适用边界 |
|---|---|---|
| 东方财富 | 既有行情/日K/分时/资金/交易事件/新闻研报/财报指标与三表关键项 | 公开免token接口，仍有断连/空记录/限流；不保证SLA |
| 新浪财经 | 快照、增补日K、资金、财务指标 | 免登录公开HTTPS；日K成交量原单位股，不把缺少成交额填0 |
| 腾讯证券 | 不复权日K | 免token公开HTTPS；偶发接口/网络失败，保留诊断 |
| 同花顺 | 增补00未复权日K | 只解析固定JSONP中的JSON，不执行JS、不解密；正常股实测成功，ST样例00曾404/超时，不拿01前复权冒充原始价格 |
| 巨潮资讯 | 新增近30天公告；不限30天的定期报告披露索引与原文链接 | 首先从公开股票映射取得orgId，不猜ID；最新财报链接不冒充已解析PDF财务数字 |
| 深圳证券交易所 | 新增第一方公司公告索引/PDF链接 | 免登录公开搜索；代码市场已确认非深市时标不适用，不计失败 |
| 上海证券交易所 | 已内置公告适配，默认关闭 | 本网络小样例一次空返回、正确股类参数请求SSL超时，未证实实际完整覆盖，不能称已验证可靠；非沪市明确不适用 |

公告多站转载相同发行人文件并不是独立财务审计。全部数据只在评估日及以前，行情请求约30交易日；事件/资讯含首尾30自然日；财报按披露日截止，不套30天。资金口径不同应分别引用，未取到不补零。

## 新窗口使用与负载保护

“多模型讨论验证”内展开“免费公开数据源管理”：启用/禁用、优先级1–100（小值先）、最近状态/股票/评估日/时间/原因、来源清单版本；手动刷新健康、检查清单、确认应用、回滚。没有建立任何定时监控/例行任务；进入页面只读取本地管理状态，不自动检查GitHub。

配置只影响下一次采集，不改历史证据。用户个人启用/排序优先于在线清单。缓存按清单版本/源/股票/评估日隔离，最多256项，保留原抓取时间；默认300秒，不伪装成刚抓的新数据。源级至少1–2秒间隔、最多四并发、最多两次瞬态重试、总预算32秒保留部分结果；连续3次真实故障熔断60秒。有效无记录/不适用不计故障。仍沿用证据不足不显示数字和概率单位契约，不改变35/20/15/15/15评分。

## 可在线更新什么 / 安全边界

固定可信JSON地址：
https://raw.githubusercontent.com/peekaboo789/limit-pattern/cursor/limit-pattern-system-8abb/backend/app/services/council/data_sources.json

- 内置清单1.0.0，schema=1，min_app_version=0.7.14；兼容旧schema1子集配置，未知schema/解析器/模板或要求更新软件则拒绝，保留旧版本。
- 只允许已审核解析器ID、已知HTTPS端点模板 `primary`、启停/顺序/2–5秒超时/30–3600秒缓存/1–5秒间隔。当前模板固定，不接受任意URL、主机、header、凭据、代码或新增Python/JS；JSON不会修复所有接口语义或解析变更。新解析器/地址模板必须正常软件更新。
- 仅从上述自家repo固定HTTPS地址获取；无重定向、无cookie/Authorization、禁环境代理凭据、拒绝DNS私网地址；最大64KiB、下载墙钟10秒、严格UTF-8/唯一JSON键/有限数字/字段与参数范围/版本检查。
- `content_sha256`=除该字段外payload的UTF-8、key排序、紧凑JSON SHA256。来源认证依靠固定仓库+HTTPS/TLS，SHA256用于完整性，不是额外独立数字签名；不宣称可抵抗可信仓库本身被攻破。
- 检查只生成10分钟有效候选，不自动应用；应用前再次验证，拒绝同版本内容突变/降级；原子replace持久化、保留上一清单供显式回滚。断网/非法清单/写入失败不替换当前配置；本地坏状态回退内置。回滚保留用户启用/排序设置。
- 维护者在自家repo仅修改JSON数据、递增清单version并重算seal的SHA256即可发布已知解析器的参数更新；不能在清单里放代码。产品三份requirements不变，不带入AKShare/pandas/efinance等新依赖。

## 真实小样例（仅公开数据采集，无付费模型，无用户实例日志）

2026-11-07 14:05–14:06 Asia/Shanghai；评估日2026-11-06，非虚构/未来日期。完整28项探测包含可选上交所：

- 600759（ST洲际）：16.48秒；{'ok': 13, 'not_applicable': 1, 'failed': 6, 'empty': 8}。有效K线30根；同日价格渠道['eastmoney', 'sina']。
  - cninfo_announcements: ok，7条；真实返回有效结构
  - cninfo_reports: ok，18条；第一方定期报告披露索引/原文链接；不是已解析完整PDF财务指标，不能冒充数值已核验
  - sina_kline: ok，30条；真实返回有效结构
  - szse_announcements: not_applicable，—条；已核验代码市场非深交所，本源不适用
  - ths_kline: failed，—条；ConnectTimeout: _ssl.c:1012: The handshake operation timed out
  - sse_announcements: failed，—条；ConnectTimeout: _ssl.c:1012: The handshake operation timed out

- 000001（平安银行，非ST）：13.98秒；{'ok': 20, 'failed': 1, 'not_applicable': 1, 'empty': 6}。有效K线30根；同日价格渠道['eastmoney', 'sina', 'tencent', 'ths']。
  - cninfo_announcements: ok，4条；真实返回有效结构
  - cninfo_reports: ok，18条；第一方定期报告披露索引/原文链接；不是已解析完整PDF财务指标，不能冒充数值已核验
  - sina_kline: ok，30条；真实返回有效结构
  - szse_announcements: ok，4条；真实返回有效结构
  - ths_kline: ok，30条；真实返回有效结构
  - sse_announcements: not_applicable，—条；已核验代码市场非上交所，本源不适用

000001同花顺/新浪/腾讯/东财同日收盘11.81；巨潮与深交所各4条公告。600759巨潮7条公告、18条近三年定期报告索引、30根新浪日K，源间收盘2.14一致；同花顺ST样例本轮超时、此前00端点404，正确保留缺失。财报数值继续由新浪/东财已披露报告补充。上交所未确认覆盖故默认关闭；不能把全部失败都称已修好。

原始公开响应/维护元数据与完整采集记录保存在/workspace/council-source-research/（不作为产品依赖或可执行插件提交）。未做用户运行实例测试或浏览器视觉验收。具体最终测试/发布结果见交付报告。
