const SCORE_LABELS: Record<string, string> = {
  amplitude: '振幅',
  turnover: '换手',
  body: 'K线实体',
  gap: '跳空缺口',
  shape: 'K线形态',
  shape_score: 'K线形态',
  volume: '成交量',
  volume_score: '成交量',
  volume_pct: '成交量百分比',
  volume_pct_score: '成交量百分比',
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
  tencent: '腾讯行情',
  sina: '新浪行情',
  eastmoney: '东方财富',
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source
}
