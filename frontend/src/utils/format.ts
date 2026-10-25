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

/** 距涨停：恒为「还差多少」的量，不带 `+` 号，否则读起来像涨幅。 */
export function formatGap(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(2)}%`
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

/** 落盘文件大小只有这一把尺子：快照卡片和别处读同一份字节数都走它。 */
export function formatBytes(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(2)} MB`
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

/** 花钱链路对用户的三句真话：请求交没交出去、这一趟按 token 计了费没有。
 *  429/5xx 那种「上游在做结论之前就把这趟挡回来」既不是「没发请求」，也不是「花了钱」。
 *  三条链路（盘中整榜、单股直评、每日固定榜）共用这一句，别在各处重写。 */
export function spendWord(value?: { request_sent?: boolean; billed?: boolean } | null): string {
  if (!value) return ''
  // 改口径之前存的那批失败档没有这两笔账。宁可不写这一句，也别拿「崩在半路」去圆一个不知道的事实。
  if (value.request_sent === undefined && value.billed === undefined) return ''
  if (!value.request_sent) {
    return value.billed === false
      ? '请求没有发出，这一次一分钱没花'
      : '这一趟崩在半路，敲没敲网关、钱花没花都不敢替它说'
  }
  if (value.billed === false) return '请求发出去了，但上游在做结论之前就把这趟挡了回来 · 这一趟没按 token 花钱'
  return '请求已发出，这一次的钱花了'
}

/** 「上一趟」那种一格宽的读数用的短版本。 */
export function spendShort(value?: { request_sent?: boolean; billed?: boolean } | null): string {
  if (!value) return '—'
  if (value.request_sent === undefined && value.billed === undefined) return '—'
  if (!value.request_sent) return value.billed === false ? '没花钱' : '说不准'
  return value.billed === false ? '上游挡回，没花钱' : '花了钱'
}
