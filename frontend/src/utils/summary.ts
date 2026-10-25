import { formatAmount, formatVolume } from './format'

export interface SectorCard {
  name: string
  count: number
  pct: number | null
}

export interface LadderCard {
  label: string
  count: number
  examples: string[]
  rank: number
}

export interface PatternCard {
  name: string
  count: number | null
  note: string
}

export interface VolumeFeature {
  key: string
  label: string
  display: string
}

export interface NormalizedSummary {
  date: string
  limitupCount: number | null
  headline: string
  sectors: SectorCard[]
  ladder: LadderCard[]
  patterns: PatternCard[]
  volumes: VolumeFeature[]
  keyPoints: string[]
  model: string
  generatedAt: string
}

const VOLUME_LABELS: Record<string, string> = {
  median_amount: '成交额中位数',
  avg_amount: '平均成交额',
  total_amount: '总成交额',
  median_turnover: '换手率中位数',
  median_turnover_rate: '换手率中位数',
  avg_turnover: '平均换手率',
  median_volume: '成交量中位数',
  sample_count: '样本数',
  early_limit_ratio: '早盘封板占比',
  sealed_ratio: '封板占比',
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function asNumber(value: unknown): number | null {
  const number = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : NaN
  return Number.isFinite(number) ? number : null
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => {
    if (typeof item === 'string') return item
    const record = asRecord(item)
    if (!record) return String(item)
    const text = record.name ?? record.text ?? record.title ?? record.code
    return text == null ? '' : String(text)
  }).filter(Boolean)
}

export function normalizeSummary(raw: unknown): NormalizedSummary {
  const record = asRecord(raw)
  const sectors = normalizeSectors(record?.sectors)
  return {
    date: record?.date == null ? '' : String(record.date),
    limitupCount: asNumber(record?.limitup_count),
    headline: record?.headline == null ? '' : String(record.headline),
    sectors,
    ladder: normalizeLadder(record?.ladder),
    patterns: normalizePatterns(record?.patterns),
    volumes: normalizeVolumes(record?.volume_features),
    keyPoints: normalizePoints(record?.key_points),
    model: record?.model == null ? '' : String(record.model),
    generatedAt: record?.generated_at == null ? '' : String(record.generated_at),
  }
}

function normalizeSectors(raw: unknown): SectorCard[] {
  if (!Array.isArray(raw)) return []
  const cards = raw.map((item, index) => {
    if (typeof item === 'string') return { name: item, count: 0, pct: null }
    const record = asRecord(item)
    if (!record) return { name: String(item), count: 0, pct: null }
    const pct = asNumber(record.pct ?? record.ratio ?? record.percent)
    return {
      name: String(record.name ?? record.sector ?? record.title ?? `板块${index + 1}`),
      count: asNumber(record.count ?? record.limitup_count ?? record.n) ?? 0,
      pct,
    }
  })
  const ratioScale = cards.length > 0 && cards.every((card) => card.pct == null || card.pct <= 1)
  if (!ratioScale) return cards
  return cards.map((card) => ({
    ...card,
    pct: card.pct == null ? null : card.pct * 100,
  }))
}

function normalizeLadder(raw: unknown): LadderCard[] {
  if (!Array.isArray(raw)) return []
  const cards = raw.map((item) => {
    if (typeof item === 'string') return { label: item, count: 0, examples: [], rank: 0 }
    const record = asRecord(item)
    if (!record) return { label: String(item), count: 0, examples: [], rank: 0 }
    const boards = asNumber(record.boards ?? record.board_count ?? record.height ?? record.level)
    const numeric =
      typeof record.boards === 'number' ||
      typeof record.board_count === 'number' ||
      typeof record.height === 'number' ||
      typeof record.level === 'number'
    const label = numeric && boards != null
      ? boards <= 1
        ? '首板'
        : `${record.at_least === true ? '≥' : ''}${boards} 连板`
      : String(record.name ?? record.label ?? record.level ?? '梯队')
    const examples = stringList(record.examples ?? record.stocks ?? record.names ?? record.samples ?? record.codes)
    return {
      label,
      count: asNumber(record.count ?? record.n) ?? examples.length,
      examples,
      rank: boards ?? 0,
    }
  })
  return cards.sort((a, b) => b.rank - a.rank || b.count - a.count)
}

function normalizePatterns(raw: unknown): PatternCard[] {
  if (!Array.isArray(raw)) return []
  return raw.map((item, index) => {
    if (typeof item === 'string') return { name: item, count: null, note: '' }
    const record = asRecord(item)
    if (!record) return { name: String(item), count: null, note: '' }
    const example = record.example_code == null ? '' : String(record.example_code)
    const note = String(record.note ?? record.description ?? record.desc ?? '')
    return {
      name: String(record.name ?? record.pattern ?? record.title ?? `形态${index + 1}`),
      count: asNumber(record.count ?? record.n),
      note: note || (example ? `例如 ${example}` : ''),
    }
  })
}

function normalizePoints(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => {
      if (typeof item === 'string') return item
      const record = asRecord(item)
      if (!record) return String(item)
      const text = record.text ?? record.content ?? record.point ?? record.title
      return text == null ? '' : String(text)
    })
    .filter(Boolean)
}

function normalizeVolumes(raw: unknown): VolumeFeature[] {
  const record = asRecord(raw)
  if (!record) return []
  return Object.entries(record).map(([key, value]) => ({
    key,
    label: VOLUME_LABELS[key] ?? key,
    display: formatFeature(key, value),
  }))
}

function formatFeature(key: string, value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'string') return value
  const number = asNumber(value)
  if (number == null) return String(value)
  if (/ratio|pct|percent/i.test(key)) {
    const pct = Math.abs(number) <= 1 ? number * 100 : number
    return `${pct.toFixed(1)}%`
  }
  if (/amount|money|turnover_amount/i.test(key)) return formatAmount(number)
  if (/volume/i.test(key) && !/turnover/i.test(key)) return formatVolume(number)
  if (/turnover/i.test(key)) return `${number.toFixed(2)}%`
  if (Number.isInteger(number)) return String(number)
  return number.toFixed(2)
}
