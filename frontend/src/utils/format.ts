const SHANGHAI = 'Asia/Shanghai'

export function formatPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(2)
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}%`
}

export function pctClass(value: number | null | undefined): 'up' | 'down' | 'flat' {
  if (value == null || !Number.isFinite(value)) return 'flat'
  if (value > 0.0001) return 'up'
  if (value < -0.0001) return 'down'
  return 'flat'
}

export function formatScore(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(1)
}

export function formatAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`
  return value.toFixed(0)
}

/** 契约未写单位。界面按 volume = 股（1 手 = 100 股）、amount = 元 展示。 */
export function formatVolume(shares: number | null | undefined): string {
  if (shares == null || !Number.isFinite(shares)) return '—'
  const lots = shares / 100
  if (Math.abs(lots) >= 1e4) return `${(lots / 1e4).toFixed(2)}万手`
  return `${Math.round(lots)}手`
}

export function formatTurnover(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(2)}%`
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const matched = value.match(/(\d{2}:\d{2})/)
  return matched?.[1] ?? value
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: SHANGHAI,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

export function shortDate(iso: string): string {
  return iso.length >= 10 ? iso.slice(5) : iso
}

export function boardLabel(board: string): string {
  if (board === 'SH') return '沪'
  if (board === 'SZ') return '深'
  return board
}

export function scoreColor(score: number): string {
  if (score >= 85) return '#e8b86d'
  if (score >= 70) return '#5ee0d0'
  if (score >= 55) return '#7eb0ff'
  return '#8ea0bb'
}

export function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, value))
}
