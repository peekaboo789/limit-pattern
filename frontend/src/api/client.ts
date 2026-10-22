import { request } from './http'
import type {
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
}
