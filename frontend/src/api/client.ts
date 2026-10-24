import { ApiError, formatDetail, request } from './http'
import type {
  AiReportListResponse,
  BoardState,
  DailyRankHistoryResponse,
  DailyRankPrepareAck,
  DailyRankRunnerStatus,
  DailyRankState,
  ExportParams,
  Health,
  KlineResponse,
  LimitUpsResponse,
  PickHistoryResponse,
  PicksCheckResponse,
  PicksListResponse,
  PicksSummary,
  PoolResponse,
  RealtimeStatus,
  RealtimeTop,
  ScanResponse,
  Settings,
  SettingsUpdate,
  SimilarityResponse,
  SingleState,
  StartAck,
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

  syncStatus: () => request<SyncStatus>('/api/sync/status'),

  limitups: (date?: string) => request<LimitUpsResponse>(`/api/limitups${query({ date })}`),

  pool: () => request<PoolResponse>('/api/pool'),

  kline: (code: string, days = 60) =>
    request<KlineResponse>(`/api/kline${query({ code, days })}`),

  similarity: (params: { code: string; date: string; top?: number }) =>
    request<SimilarityResponse>(
      `/api/similarity${query({ code: params.code, date: params.date, top: params.top ?? 20 })}`,
    ),

  scan: (params: { date: string; top?: number; min_score?: number }) =>
    request<ScanResponse>(
      `/api/scan${query({
        date: params.date,
        top: params.top ?? 50,
        min_score: params.min_score ?? 0,
      })}`,
    ),

  summaries: () => request<SummaryListResponse>('/api/summaries'),

  summary: (date: string) => request<SummaryDocument>(`/api/summaries/${date}`),

  generateSummary: (date: string) =>
    request<SummaryDocument>(`/api/summaries/${date}/generate`, { method: 'POST' }),

  realtimeTop: (params: { metric?: string; top_n?: number } = {}) =>
    request<RealtimeTop>(`/api/realtime/top${query({ metric: params.metric, top_n: params.top_n })}`),

  realtimeStatus: () => request<RealtimeStatus>('/api/realtime/status'),

  startBoardAnalysis: () => request<StartAck>('/api/ai/analysis', { method: 'POST' }),

  boardAnalysis: () => request<BoardState>('/api/ai/analysis'),

  startSingleAnalysis: (code: string) =>
    request<StartAck>(`/api/ai/single/${code}`, { method: 'POST' }),

  singleAnalysis: (code: string) => request<SingleState>(`/api/ai/single/${code}`),

  aiReports: (params: { chain?: string; limit?: number } = {}) =>
    request<AiReportListResponse>(`/api/ai/reports${query({ chain: params.chain, limit: params.limit ?? 20 })}`),

  pickHistory: (code: string) => request<PickHistoryResponse>(`/api/ai/picks/${code}`),

  picksList: (params: { date?: string; verdict?: string; limit?: number } = {}) =>
    request<PicksListResponse>(
      `/api/ai/picks${query({ date: params.date, verdict: params.verdict, limit: params.limit ?? 200 })}`,
    ),

  picksSummary: () => request<PicksSummary>('/api/ai/picks/summary'),

  picksCheck: () => request<PicksCheckResponse>('/api/ai/picks/check', { method: 'POST' }),

  dailyRank: (date?: string) => request<DailyRankState>(`/api/ai/daily-rank${query({ date })}`),

  dailyRankStatus: () => request<DailyRankRunnerStatus>('/api/ai/daily-rank/status'),

  dailyRankHistory: (limit = 15) =>
    request<DailyRankHistoryResponse>(`/api/ai/daily-rank/history${query({ limit })}`),

  dailyRankPrepare: () => request<DailyRankPrepareAck>('/api/ai/daily-rank/prepare', { method: 'POST' }),

  dailyRankRank: (date?: string) =>
    request<StartAck>(`/api/ai/daily-rank/rank${query({ date })}`, { method: 'POST' }),

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
