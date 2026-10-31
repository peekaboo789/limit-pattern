import { ApiError } from '@/api/http'
import type { Health, Settings, SummaryDocument, SyncStatus } from '@/api/types'
import {
  DATA_DATE,
  buildScan,
  buildSimilarity,
  buildSummary,
  klineOf,
  limitupsOn,
  poolDates,
  poolItems,
  summaryDates,
} from './market'

const settings: Settings = {
  retention_trading_days: 5,
  compare_window_trading_days: 3,
  ai_enabled: true,
  ai_base_url: 'https://api.deepseek.com/v1',
  ai_model: 'deepseek-chat',
  ai_api_key_set: true,
  data_source: 'tencent',
  last_sync_at: '2026-10-22T15:05:12+08:00',
  last_cleanup_at: '2026-10-22T15:10:04+08:00',
}

const health: Health = {
  status: 'ok',
  version: '0.5.0',
  data_date: DATA_DATE,
}

const syncState: SyncStatus = {
  running: false,
  progress: 100,
  message: '已与最近交易日对齐',
  last_sync_at: settings.last_sync_at,
}

let syncTaskId = 'sync-initial'
let syncTimer = 0
const summaryOverrides = new Map<string, SummaryDocument>()

function wait(ms: number, signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function readBody(init?: RequestInit): unknown {
  if (!init?.body) return null
  if (typeof init.body !== 'string') throw new ApiError(400, '无法解析请求体')
  try {
    return JSON.parse(init.body) as unknown
  } catch {
    throw new ApiError(400, '请求体不是合法 JSON')
  }
}

function requireDate(value: string | null, fallback: string): string {
  const date = value && value.trim() ? value.trim() : fallback
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new ApiError(422, '日期格式应为 YYYY-MM-DD')
  return date
}

function requireCode(value: string | null): string {
  const code = value?.trim() ?? ''
  if (!/^\d{6}$/.test(code)) throw new ApiError(422, '股票代码应为 6 位数字')
  return code
}

function requireInt(value: string | null, fallback: number, name: string, min: number, max: number): number {
  if (value == null || value === '') return fallback
  const number = Number(value)
  if (!Number.isInteger(number) || number < min || number > max) {
    throw new ApiError(422, `${name}超出范围`)
  }
  return number
}

function cloneSettings(): Settings {
  return { ...settings }
}

function updateSettings(body: unknown): Settings {
  if (!body || typeof body !== 'object') throw new ApiError(400, '请求体不是 JSON 对象')
  const input = body as Record<string, unknown>
  if ('retention_trading_days' in input) {
    const value = input.retention_trading_days
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
      throw new ApiError(422, '保留天数必须是正整数')
    }
    settings.retention_trading_days = value
  }
  if ('compare_window_trading_days' in input) {
    const value = input.compare_window_trading_days
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
      throw new ApiError(422, '对比窗口必须是正整数')
    }
    settings.compare_window_trading_days = value
  }
  if ('ai_enabled' in input) {
    if (typeof input.ai_enabled !== 'boolean') throw new ApiError(422, 'ai_enabled 必须是布尔值')
    settings.ai_enabled = input.ai_enabled
  }
  if ('ai_base_url' in input) {
    if (typeof input.ai_base_url !== 'string') throw new ApiError(422, 'AI 接口地址格式不正确')
    settings.ai_base_url = input.ai_base_url.trim()
  }
  if ('ai_model' in input) {
    if (typeof input.ai_model !== 'string') throw new ApiError(422, '模型名称格式不正确')
    settings.ai_model = input.ai_model.trim()
  }
  if ('data_source' in input) {
    if (typeof input.data_source !== 'string' || !input.data_source.trim()) {
      throw new ApiError(422, '数据源不能为空')
    }
    settings.data_source = input.data_source.trim()
  }
  if (typeof input.ai_api_key === 'string' && input.ai_api_key.trim()) {
    settings.ai_api_key_set = true
  }
  if (settings.ai_enabled && (!settings.ai_base_url || !settings.ai_model)) {
    throw new ApiError(422, '开启 AI 时需要填写接口地址和模型')
  }
  return cloneSettings()
}

function startSync(): { task_id: string } {
  if (syncState.running) return { task_id: syncTaskId }
  syncTaskId = `sync-${Date.now()}`
  syncState.running = true
  syncState.progress = 6
  syncState.message = '连接行情源…'
  const steps: Array<[number, string]> = [
    [22, '连接行情源…'],
    [46, '拉取沪市行情…'],
    [68, '拉取深市行情…'],
    [86, '整理涨停样本…'],
    [100, '同步完成'],
  ]
  let index = 0
  if (syncTimer) clearInterval(syncTimer)
  syncTimer = window.setInterval(() => {
    const step = steps[index]
    if (!step) return
    syncState.progress = step[0]
    syncState.message = step[1]
    index += 1
    if (syncState.progress >= 100) {
      clearInterval(syncTimer)
      syncTimer = 0
      syncState.running = false
      syncState.message = `同步完成，数据日期 ${health.data_date ?? DATA_DATE}`
      syncState.last_sync_at = new Date().toISOString()
      settings.last_sync_at = syncState.last_sync_at
    }
  }, 380)
  return { task_id: syncTaskId }
}

function summaryFor(date: string): SummaryDocument {
  if (!summaryDates().includes(date)) throw new ApiError(404, '没有该日总结')
  return summaryOverrides.get(date) ?? buildSummary(date, settings.ai_model)
}

function listSummaries(): { items: Array<{ date: string; limitup_count: number; headline: string }> } {
  const items = [...summaryDates()]
    .reverse()
    .map((date) => {
      const document = summaryFor(date)
      return {
        date,
        limitup_count: document.limitup_count,
        headline: document.headline ?? '',
      }
    })
  return { items }
}

function generateSummary(date: string): SummaryDocument {
  if (!summaryDates().includes(date)) throw new ApiError(404, '没有该日总结')
  if (!settings.ai_enabled || !settings.ai_api_key_set || !settings.ai_base_url || !settings.ai_model) {
    throw new ApiError(409, '未配置AI接口')
  }
  const document = buildSummary(date, settings.ai_model)
  document.generated_at = new Date().toISOString()
  document.key_points = [...document.key_points.filter((item) => item !== '本次为重新生成的复盘，不改变行情事实。'), '本次为重新生成的复盘，不改变行情事实。']
  summaryOverrides.set(date, document)
  return document
}

function handle(method: string, pathname: string, params: URLSearchParams, init?: RequestInit): unknown {
  if (method === 'GET' && pathname === '/api/health') return { ...health }
  if (method === 'GET' && pathname === '/api/settings') return cloneSettings()
  if (method === 'PUT' && pathname === '/api/settings') return updateSettings(readBody(init))
  if (method === 'POST' && pathname === '/api/sync') return startSync()
  if (method === 'GET' && pathname === '/api/sync/status') return { ...syncState }
  if (method === 'GET' && pathname === '/api/limitups') {
    const date = requireDate(params.get('date'), health.data_date ?? DATA_DATE)
    const items = limitupsOn(date).map((item) => ({
      code: item.code,
      name: item.name,
      board: item.board,
      close: item.close,
      pct_chg: item.pct_chg,
      volume: item.volume,
      amount: item.amount,
      turnover_rate: item.turnover_rate,
      consecutive_boards: item.consecutive_boards,
      first_limit_time: item.first_limit_time,
      limit_type: item.limit_type,
    }))
    return { date, items }
  }
  if (method === 'GET' && pathname === '/api/pool') {
    const dates = poolDates()
    return { dates, items: poolItems(dates) }
  }
  if (method === 'GET' && pathname === '/api/kline') {
    const code = requireCode(params.get('code'))
    const days = requireInt(params.get('days'), 30, 'days', 1, 200)
    return klineOf(code, days)
  }
  if (method === 'GET' && pathname === '/api/similarity') {
    const code = requireCode(params.get('code'))
    const date = requireDate(params.get('date'), health.data_date ?? DATA_DATE)
    const top = requireInt(params.get('top'), 20, 'top', 1, 100)
    return buildSimilarity(code, date, top, settings.compare_window_trading_days)
  }
  if (method === 'GET' && pathname === '/api/scan') {
    const date = requireDate(params.get('date'), health.data_date ?? DATA_DATE)
    const top = requireInt(params.get('top'), 50, 'top', 1, 200)
    const minScore = Number(params.get('min_score') ?? 0)
    if (!Number.isFinite(minScore) || minScore < 0 || minScore > 100) throw new ApiError(422, 'min_score 超出范围')
    return buildScan(date, top, minScore)
  }
  if (method === 'GET' && pathname === '/api/summaries') return listSummaries()
  const generate = pathname.match(/^\/api\/summaries\/(\d{4}-\d{2}-\d{2})\/generate$/)
  if (generate && method === 'POST') return generateSummary(generate[1] ?? '')
  const detail = pathname.match(/^\/api\/summaries\/(\d{4}-\d{2}-\d{2})$/)
  if (detail && method === 'GET') return summaryFor(detail[1] ?? '')
  throw new ApiError(404, '接口不存在')
}

export function mockRuntime(): { compareWindow: number; model: string } {
  return {
    compareWindow: settings.compare_window_trading_days,
    model: settings.ai_model,
  }
}

export function currentSummary(date: string): SummaryDocument {
  return summaryFor(date)
}

export async function mockRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  await wait(140, init?.signal)
  const url = new URL(path, 'http://127.0.0.1')
  return handle(method, url.pathname, url.searchParams, init) as T
}
