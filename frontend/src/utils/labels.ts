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
  local: '本地库',
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

/** 盘中时段：`clock.session_phase()` 的三个键。 */
const PHASE_LABELS: Record<string, string> = {
  pre_open: '开盘前',
  morning: '上午盘中',
  frozen: '上午已收',
}

export function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return '—'
  return PHASE_LABELS[phase] ?? phase
}

/** 形态窗口口径：`realtime_service.WINDOW_*`。 */
const WINDOW_MODE_LABELS: Record<string, string> = {
  intraday_morning: '含今天上午半日线',
  settled: '只用收盘日线',
}

export function windowModeLabel(mode: string | null | undefined): string {
  if (!mode) return '—'
  return WINDOW_MODE_LABELS[mode] ?? mode
}

/** 快照从哪来：`realtime_service.SOURCE_*`。 */
const SNAPSHOT_SOURCE_LABELS: Record<string, string> = {
  realtime: '实时快照',
  settled: '收盘回放',
}

export function snapshotSourceLabel(source: string | null | undefined): string {
  if (!source) return '—'
  return SNAPSHOT_SOURCE_LABELS[source] ?? source
}

export function sourceLabel(source: string): string {
  if (!source) return ''
  if (source.includes(',')) return source.split(',').map((item) => sourceLabel(item.trim())).filter(Boolean).join('、')
  return SOURCE_LABELS[source] ?? source
}
