import { ApiError } from '@/api/http'
import type { CouncilProgress, CouncilSession, Health, Settings, SummaryDocument, SyncStatus } from '@/api/types'
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
  version: '0.6.0',
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

const councilSessions: CouncilSession[] = [
  {
    id: 1,
    stock_code: '600519',
    stock_name: '贵州茅台',
    eval_date: DATA_DATE,
    status: 'done',
    participants: [{ id: 'm1', name: 'DeepSeek', model: 'deepseek-chat' }, { id: 'm2', name: 'Qwen', model: 'qwen-plus' }],
    evidence: {
      local: {
        code: '600519',
        name: '贵州茅台',
        exchange: 'SSE',
        board: 'main_board',
        as_of: DATA_DATE,
        chart: {
          source: 'local',
          bars: Array.from({ length: 20 }, (_, i) => {
            const base = 1680 + i
            const open = base + (i % 2 ? 4 : -3)
            const close = base + (i % 3 ? 5 : -2)
            return {
              date: `2026-0${6 + Math.floor(i / 9)}-${String((i % 9) + 2).padStart(2, '0')}`,
              open,
              high: Math.max(open, close) + 6,
              low: Math.min(open, close) - 6,
              close,
              volume: 18000000 + i * 400000,
              amount: 3.1e9 + i * 4e7,
              pct_chg: i % 4 === 0 ? -0.6 : 0.7,
              turnover_rate: 0.2 + (i % 5) / 100,
              is_limit_up: false,
            }
          }),
        },
        bars_30d: [],
        history: { available: true, picks: 4, hit: 2, miss: 1, pending: 1 },
      },
      market: { date: DATA_DATE, stocks: 5123, up: 2980, down: 1950, limit_up: 61, limit_down: 3, broken: 9 },
      web: [
        { status: 'ok', category: 'market', source: 'eastmoney_spot', url: 'https://example.com/spot', fetched_at: new Date().toISOString(), window: { start: '2026-10-08', end: '2026-11-07', days: 31 }, digest: { code: '600519', name: '贵州茅台', price: 1694.05, pct_chg: 0.86 } },
        { status: 'ok', category: 'capital', source: 'eastmoney_capital_flow', url: 'https://example.com/fflow', fetched_at: new Date().toISOString(), window: { start: '2026-10-08', end: '2026-11-07', days: 31 }, digest: { count: 20, rows: [{ date: '2026-11-06', main_net: 32400000, super_net: 18000000 }] } },
        { status: 'empty', category: 'trade', source: 'eastmoney_block_trade', url: 'https://example.com/block', fetched_at: new Date().toISOString(), window: { start: '2026-10-08', end: '2026-11-07', days: 31 }, digest: { note: '近 30 天未取到大宗交易' } },
        { status: 'ok', category: 'news', source: 'eastmoney_news', url: 'https://example.com/news', fetched_at: new Date().toISOString(), window: { start: '2026-10-08', end: '2026-11-07', days: 31 }, digest: { count: 3, items: [{ title: '示例新闻标题', date: '2026-11-06', source: '东方财富', url: 'https://example.com/a' }] } },
        { status: 'ok', category: 'financial', source: 'eastmoney_financial_main', url: 'https://example.com/f10', fetched_at: new Date().toISOString(), window: { start: '2026-10-08', end: '2026-11-07', days: 31 }, digest: { latest: { report_date: '2026-09-30', disclosure_date: '2026-10-25', revenue: 92_760_000_000, revenue_yoy: 8.4, net_profit: 41_300_000_000, net_profit_yoy: 7.2, eps: 32.85, roe: 24.1, gross_margin: 91.9, debt_asset_ratio: 24.3 } } },
      ],
    },
    statements: [
      { index: 0, speaker: 'DeepSeek', model: 'deepseek-chat', content: '本地行情与联网快照基本一致。', error: '' },
      { index: 1, speaker: 'Qwen', model: 'qwen-plus', content: '{"up_probability":54,"confidence":"medium","summary":"测试总结"}', error: '' },
    ],
    conclusion: {
      up_probability: 54,
      confidence: 'medium',
      verified: ['收盘价一致'],
      conflicts_or_unverified: [],
      agreements: ['本地与联网一致'],
      disagreements: [],
      bullish: ['收盘价一致'],
      bearish: [],
      risks: ['示例风险'],
      cne6_notes: ['Momentum 未给出'],
      summary: '模拟讨论：证据基本一致，次日上涨概率为估计。',
      disclaimer: '这是讨论会估计，不计入系统综合评分，不是确定预测，不是投资建议。',
    },
    error: '',
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
  },
]
let councilProgress: CouncilProgress = { running: false, phase: 'idle', session_id: null, stock: null, stage: '空闲', current_model: '', done: 0, total: 0, last_status: '', error: '' }
let councilOrder: string[] = ['m1', 'm2']

function councilList(): { items: CouncilSession[] } {
  return { items: councilSessions }
}

function councilRun(body: Record<string, unknown>): { session_id: number; started: boolean; participants: CouncilSession['participants'] } {
  const code = String(body.code ?? '')
  if (!/^\d{6}$/.test(code)) throw new ApiError(422, '股票代码应为 6 位数字')
  const participants = [{ id: 'm1', name: 'DeepSeek', model: 'deepseek-chat' }, { id: 'm2', name: 'Qwen', model: 'qwen-plus' }]
  councilProgress = { running: true, phase: 'verifying_web', session_id: 2, stock: code, stage: '联网核对', current_model: '', done: 0, total: 2, last_status: '正在联网核对', error: '' }
  return { session_id: 2, started: true, participants }
}

function councilDetail(id: number): CouncilSession {
  if (id === 1) return councilSessions[0]
  return {
    id,
    stock_code: councilProgress.stock ?? '600519',
    stock_name: '贵州茅台',
    eval_date: DATA_DATE,
    status: councilProgress.running ? 'running' : 'done',
    participants: [{ id: 'm1', name: 'DeepSeek', model: 'deepseek-chat' }, { id: 'm2', name: 'Qwen', model: 'qwen-plus' }],
    evidence: { local: { code: councilProgress.stock ?? '600519' }, web: [] },
    statements: [],
    conclusion: null,
    error: '',
    created_at: new Date().toISOString(),
    finished_at: null,
  }
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
  if (method === 'POST' && pathname === '/api/council/run') return councilRun(readBody(init) as Record<string, unknown>)
  if (method === 'GET' && pathname === '/api/council/progress') return { ...councilProgress }
  if (method === 'GET' && pathname === '/api/council/history') return councilList()
  if (method === 'GET' && pathname === '/api/council') return councilList()
  if (method === 'GET' && pathname === '/api/council/profile-order') return { profile_ids: councilOrder }
  if (method === 'PUT' && pathname === '/api/council/profile-order') {
    const body = readBody(init) as { profile_ids?: string[] }
    councilOrder = body.profile_ids ?? councilOrder
    return { profile_ids: councilOrder }
  }
  const councilDetailMatch = pathname.match(/^\/api\/council\/(\d+)$/)
  if (councilDetailMatch && method === 'GET') return councilDetail(Number(councilDetailMatch[1] ?? 0))
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
