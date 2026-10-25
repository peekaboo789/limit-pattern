const SCORE_LABELS: Record<string, string> = {
  amplitude: '振幅',
  turnover: '换手',
  body: 'K线实体',
  gap: '跳空缺口',
  shape: 'K线形态',
  shape_score: 'K线形态',
  volume: '成交量',
  volume_score: '成交量',
  volume_pct: '量比',
  volume_pct_score: '量比',
  amount: '成交额',
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
  auto: '自动（东财 → 腾讯 → 新浪）',
  eastmoney: '东方财富',
  tencent: '腾讯行情',
  sina: '新浪行情',
}

/** AI 推荐对完账后的三种判定。`ai_picks.VERDICT_HIT/MISS/PENDING` 就是这三个键，
 *  中文只在这里映射一次：详情页和别处读同一份账，不能各写一套说法。 */
const VERDICT_LABELS: Record<string, string> = {
  hit: '命中',
  miss: '没命中',
  pending: '还待验证',
}

export function verdictLabel(verdict: string): string {
  return VERDICT_LABELS[verdict] ?? verdict
}

export function verdictClass(verdict: string): string {
  if (verdict === 'hit') return 'tag-hot'
  if (verdict === 'miss') return 'type-weak'
  return 'type-normal'
}

export function sourceLabel(source: string): string {
  if (!source) return ''
  if (source.includes(',')) return source.split(',').map((item) => sourceLabel(item.trim())).filter(Boolean).join('、')
  return SOURCE_LABELS[source] ?? source
}
