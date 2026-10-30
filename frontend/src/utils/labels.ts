const SCORE_LABELS: Record<string, string> = {
  amplitude: '振幅',
  body: 'K线实体',
  gap: '跳空缺口',
  shape: 'K线形态',
  shape_score: 'K线形态',
  volume: '成交量 15%',
  volume_score: '成交量',
  volume_pct: '量比',
  volume_pct_score: '量比',
  amount: '成交额 10%',
  price_trend: '价格走势 35%',
  kline_structure: 'K线结构 20%',
  turnover: '换手 10%',
  position: '历史位置 5%',
  three_day_trend: '三日趋势 5%',
}

export function scoreLabel(key: string): string {
  return SCORE_LABELS[key] ?? key
}

export function limitTypeClass(type: string): string {
  if (type.includes('一字')) return 'type-yizi'
  if (type.includes('T')) return 'type-t'
  if (type.includes('回封')) return 'type-reseal'
  if (type.includes('烂')) return 'type-weak'
  return 'type-normal'
}

const SOURCE_LABELS: Record<string, string> = {
  auto: '自动（新浪 → 腾讯 → 东财）',
  eastmoney: '东方财富',
  tencent: '腾讯行情',
  sina: '新浪行情',
}

export function sourceLabel(source: string): string {
  if (!source) return ''
  if (source.includes(',')) return source.split(',').map((item) => sourceLabel(item.trim())).filter(Boolean).join('、')
  return SOURCE_LABELS[source] ?? source
}
