import { ApiError } from '@/api/http'
import type { ExportParams } from '@/api/types'
import { formatAmount, formatPct, formatPrice, formatScore } from '@/utils/format'
import { defaultExportFilename } from '@/utils/saveFile'
import { buildScan, buildSimilarity, poolDates, poolItems } from './market'
import { currentSummary, mockRuntime } from './server'

const DISCLAIMER = '相似度不是涨停概率'

function header(title: string, rows: string[]): string {
  return [`${DISCLAIMER}`, '【模拟数据】正式环境的正文以后端 /api/export 为准。', title, ...rows, ''].join('\n')
}

function requireDate(date: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new ApiError(422, '日期格式应为 YYYY-MM-DD')
  return date
}

function requireTop(top: number | undefined, fallback: number): number {
  if (top == null) return fallback
  if (!Number.isInteger(top) || top < 1 || top > 200) throw new ApiError(422, 'top 超出范围')
  return top
}

export function mockExportText(params: ExportParams): { filename: string; text: string } {
  const date = requireDate(params.date)
  const filename = defaultExportFilename(params.type, date, params.code, params.top)
  if (params.type === 'scan') return { filename, text: scanText(date, requireTop(params.top, 50)) }
  if (params.type === 'similarity') return { filename, text: similarityText(date, params.code, requireTop(params.top, 20)) }
  if (params.type === 'pool') return { filename, text: poolText(date) }
  if (params.type === 'summary') return { filename, text: summaryText(date) }
  if (params.type === 'next_day_pool' || params.type === 'backtest') return { filename, text: `模拟数据没有${params.type}\n` }
  if (params.type === 'ai_ranking') return { filename, text: `AI 榜单 ${date}（模拟数据没有榜单）\n` }
  throw new ApiError(422, 'type 必须是 scan、similarity、pool 或 summary')
}

function scanText(date: string, top: number): string {
  const scan = buildScan(date, top, 0)
  const lines = scan.items.map((item, index) =>
    [
      index + 1,
      item.code,
      item.name,
      formatScore(item.score),
      item.best_match.code,
      item.best_match.name,
      item.best_match.limit_date,
      formatScore(item.shape_score),
      formatScore(item.volume_score),
      formatScore(item.volume_pct_score),
    ].join('\t'),
  )
  return header(`扫描排行  ${scan.date}  前 ${top} 条  样本池 ${scan.pool_size}`, [
    '排名\t代码\t名称\t综合相似度\t最相似代码\t最相似名称\t涨停日\t形态\t成交量\t成交量百分比',
    ...lines,
  ])
}

function similarityText(date: string, code: string | undefined, top: number): string {
  if (!code || !/^\d{6}$/.test(code)) throw new ApiError(422, '导出相似度需要 6 位股票代码')
  const { compareWindow } = mockRuntime()
  const result = buildSimilarity(code, date, top, compareWindow)
  const lines = result.matches.map((match, index) => {
    const extra = Object.entries(match.other_scores)
      .map(([key, value]) => `${key}=${formatScore(value)}`)
      .join(',')
    return [
      index + 1,
      match.code,
      match.name,
      match.limit_date,
      formatScore(match.score),
      formatScore(match.shape_score),
      formatScore(match.volume_score),
      formatScore(match.volume_pct_score),
      match.window.join(','),
      extra,
    ].join('\t')
  })
  return header(`个股相似度  ${result.target.name} ${result.target.code}`, [
    `截止日期\t${date}`,
    `对比窗口\t${result.target.window.join(',')}`,
    `股票池\t${result.pool_dates.join(',')}  共 ${result.pool_size} 条，已排除自身`,
    '排名\t代码\t名称\t涨停日\t综合相似度\t形态\t成交量\t成交量百分比\t样本窗口\t其他分项',
    ...lines,
  ])
}

function poolText(date: string): string {
  const dates = poolDates()
  const items = poolItems(dates).filter((item) => item.limit_date === date)
  const lines = items.map((item) =>
    [
      item.limit_date,
      item.code,
      item.name,
      item.board,
      formatPrice(item.close),
      formatPct(item.pct_chg),
      item.consecutive_boards,
      item.first_limit_time ?? '',
      item.limit_type,
      item.turnover_rate.toFixed(2),
      formatAmount(item.amount),
    ].join('\t'),
  )
  return header(`涨停池  ${date}`, [
    `近三日\t${dates.join(',')}`,
    items.length ? `当日 ${items.length} 只` : '该日不在近三日涨停池，或当日没有涨停样本。',
    '涨停日\t代码\t名称\t市场\t收盘\t涨幅\t连板\t封板时间\t形态\t换手%\t成交额',
    ...lines,
  ])
}

function summaryText(date: string): string {
  const document = currentSummary(date)
  const sectors = Array.isArray(document.sectors)
    ? document.sectors.map((item) => {
        if (!item || typeof item !== 'object') return String(item)
        const row = item as { name?: unknown; count?: unknown; pct?: unknown }
        return `${String(row.name ?? '')}\t${String(row.count ?? '')}\t${row.pct ?? ''}%`
      })
    : []
  const ladder = Array.isArray(document.ladder)
    ? document.ladder.map((item) => {
        if (!item || typeof item !== 'object') return String(item)
        const row = item as { boards?: unknown; count?: unknown; examples?: unknown }
        const examples = Array.isArray(row.examples) ? row.examples.map(String).join('、') : ''
        return `${String(row.boards ?? '')}板\t${String(row.count ?? '')}\t${examples}`
      })
    : []
  const patterns = Array.isArray(document.patterns)
    ? document.patterns.map((item) => {
        if (!item || typeof item !== 'object') return String(item)
        const row = item as { name?: unknown; count?: unknown; note?: unknown }
        return `${String(row.name ?? '')}\t${String(row.count ?? '')}\t${String(row.note ?? '')}`
      })
    : []
  const points = Array.isArray(document.key_points) ? document.key_points.map((item, index) => `${index + 1}. ${String(item)}`) : []
  const volumeLines = Object.entries(document.volume_features).map(([key, value]) => `${key}\t${value ?? ''}`)
  return header(`每日总结  ${document.date}  ${document.headline ?? ''}`, [
    `涨停家数\t${document.limitup_count}`,
    `模型\t${document.model}`,
    `生成时间\t${document.generated_at}`,
    '',
    '要点',
    ...points,
    '',
    '板块\t数量\t占比',
    ...sectors,
    '',
    '连板\t数量\t例子',
    ...ladder,
    '',
    '形态\t数量\t代表',
    ...patterns,
    '',
    '量能',
    ...volumeLines,
  ])
}
