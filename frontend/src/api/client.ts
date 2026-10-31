import { ApiError, formatDetail, request } from './http'
import type { CaptureStatus, ManualResult,
  SourceTestResult,
  ClusterResult,
  CalendarInfo,
  NextDayPool,
  RankingBacktest,
  SimilarityBacktest,
  AiRanking,
  MonitorEvents,
  MonitorSnapshot,
  ExportParams,
  Health,
  KlineResponse,
  LimitUpsResponse,
  PoolResponse,
  ScanResponse,
  Settings,
  SettingsUpdate,
  SimilarityResponse,
  SummaryDocument,
  SummaryListResponse,
  SyncStatus,
  SyncTask,
} from './types'
import { defaultExportFilename, filenameFromDisposition } from '@/utils/saveFile'

export { ApiError, isAbort } from './http'

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  health: () => request<Health>('/api/health'),

  settings: () => request<Settings>('/api/settings'),

  saveSettings: (body: SettingsUpdate) =>
    request<Settings>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  startSync: () => request<SyncTask>('/api/sync', { method: 'POST' }),

  startFullSync: (days: number) =>
    request<SyncTask & { days: number; note: string; retention_trading_days: number }>('/api/sync/full', {
      method: 'POST',
      body: JSON.stringify({ days }),
    }),

  testAi: (body: { ai_base_url?: string; ai_model?: string; ai_api_key?: string; ai_thinking?: string }) =>
    request<{ ok: boolean; error?: string; reply?: string; endpoint: string; note?: string; latency_ms: number; thinking?: string }>(
      '/api/ai/test',
      { method: 'POST', body: JSON.stringify(body) },
      360000,
    ),

  cancelSync: () => request<SyncStatus>('/api/sync/cancel', { method: 'POST' }),

  testSources: (timeout = 8) =>
    request<{ timeout: number; results: SourceTestResult[] }>('/api/sources/test', {
      method: 'POST',
      body: JSON.stringify({ timeout }),
    }, (timeout + 6) * 1000),

  syncStatus: () => request<SyncStatus>('/api/sync/status'),

  limitups: (date?: string) => request<LimitUpsResponse>(`/api/limitups${query({ date })}`),

  pool: (date?: string) => request<PoolResponse>(`/api/pool${query({ date })}`, undefined, 60000),

  kline: (code: string, days = 60) =>
    request<KlineResponse>(`/api/kline${query({ code, days })}`),

  similarity: (params: { code: string; date: string; top?: number }) =>
    request<SimilarityResponse>(
      `/api/similarity${query({ code: params.code, date: params.date, top: params.top ?? 20 })}`,
      undefined,
      120000,
    ),

  scan: (params: { date: string; top?: number; min_score?: number }) =>
    request<ScanResponse>(
      `/api/scan${query({
        date: params.date,
        top: params.top ?? 50,
        min_score: params.min_score ?? 0,
      })}`,
      undefined,
      120000,
    ),

  summaries: () => request<SummaryListResponse>('/api/summaries'),

  summary: (date: string) => request<SummaryDocument>(`/api/summaries/${date}`),

  generateSummary: (date: string) =>
    request<SummaryDocument>(`/api/summaries/${date}/generate`, { method: 'POST' }, 180000),

  aiRanking: (date?: string) => request<AiRanking>(`/api/ai-ranking${query({ date })}`),

  aiRankingDates: () => request<{ dates: string[] }>('/api/ai-ranking/dates'),

  generateAiRanking: (options: { date?: string; snapshot?: 'morning' | 'manual' } = {}) =>
    request<AiRanking>('/api/ai-ranking/generate', { method: 'POST', body: JSON.stringify(options) }, 3600000),

  aiRankingProgress: () =>
    request<{ running: boolean; done: number; total: number; failed: number; split?: number; scored?: number; batch_size?: number }>('/api/ai-ranking/progress', {}, 5000),

  clusterStatus: () => request<{ available: boolean; message: string }>('/api/cluster/status'),

  cluster: (date?: string) => request<ClusterResult>(`/api/cluster${query({ date })}`),

  runCluster: (body: { date?: string; k: number | 'auto'; method: string; interpret?: boolean }) =>
    request<ClusterResult>('/api/cluster/run', { method: 'POST', body: JSON.stringify(body) }, 180000),

  calendar: (year?: number) => request<CalendarInfo>(`/api/calendar${query({ year })}`),

  similarityBacktest: (top = 50) => request<SimilarityBacktest>(`/api/backtest${query({ kind: 'similarity', top })}`, undefined, 120000),

  rankingBacktest: () => request<RankingBacktest>('/api/backtest?kind=ai_ranking'),

  nextDayPool: (top = 30) => request<NextDayPool>(`/api/next-day-pool${query({ top })}`, undefined, 120000),

  startCapture: (fallback = false) =>
    request<ManualResult | CaptureStatus>('/api/snapshot/capture', { method: 'POST', body: JSON.stringify(fallback ? { fallback: true } : {}) }, 120000),
  backtestWarm: () => request<{ running: boolean; last_seconds: number | null; last_error: string }>('/api/backtest/warm', {}, 5000),
  captureStatus: () => request<CaptureStatus>('/api/snapshot/capture/status', {}, 5000),
  cancelCapture: () => request<CaptureStatus>('/api/snapshot/capture/cancel', { method: 'POST' }),
  manualSnapshot: (date?: string) => request<ManualResult>(`/api/snapshot/manual${query({ date })}`),

  monitor: () => request<MonitorSnapshot>('/api/monitor'),

  monitorEvents: (since: number) => request<MonitorEvents>(`/api/monitor/events${query({ since })}`),

  aiInput: async (format: 'txt' | 'md', date?: string, snapshot?: 'manual') => {
    const response = await fetch(`/api/ai-input${query({ format, date, top: 100, snapshot })}`)
    const text = await response.text()
    if (!response.ok) {
      let detail = text.trim() || `导出失败（${response.status}）`
      try {
        const parsed = JSON.parse(text) as { detail?: unknown }
        if (parsed && typeof parsed === 'object' && 'detail' in parsed) detail = formatDetail(parsed.detail)
      } catch {
        // 纯文本错误
      }
      throw new ApiError(response.status, detail)
    }
    const filename = filenameFromDisposition(response.headers.get('Content-Disposition')) ?? `ai_input.${format}`
    return { filename, text, blob: new Blob([text], { type: 'text/plain;charset=utf-8' }) }
  },
  importAiRanking: (text: string, date?: string) =>
    request<AiRanking>('/api/ai-ranking/import', { method: 'POST', body: JSON.stringify({ text, date }) }, 120000),
  exportText: async (params: ExportParams) => {
    if (import.meta.env.VITE_USE_MOCK === '1') {
      const { mockExportText } = await import('@/mocks/exportText')
      const file = mockExportText(params)
      return {
        filename: file.filename,
        text: file.text,
        blob: new Blob([file.text], { type: 'text/plain;charset=utf-8' }),
      }
    }

    const response = await fetch(
      `/api/export${query({
        type: params.type,
        date: params.date,
        code: params.code,
        top: params.top,
      })}`,
    )
    const blob = await response.blob()
    if (!response.ok) {
      const raw = await blob.text()
      let detail = raw.trim() || `导出失败（${response.status}）`
      try {
        const parsed = JSON.parse(raw) as { detail?: unknown }
        if (parsed && typeof parsed === 'object' && 'detail' in parsed) detail = formatDetail(parsed.detail)
      } catch {
        // 失败响应有时是纯文本，而不是 {"detail": "..."}。
      }
      throw new ApiError(response.status, detail)
    }
    const text = await blob.text()
    const filename =
      filenameFromDisposition(response.headers.get('Content-Disposition')) ??
      defaultExportFilename(params.type, params.date, params.code, params.top)
    return { filename, text, blob }
  },
}
