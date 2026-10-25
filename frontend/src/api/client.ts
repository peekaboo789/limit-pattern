import { ApiError, formatDetail, plainDetail, request } from './http'
import type {
  AiReportListResponse,
  AiMemoryDays,
  AiMemoryRunResult,
  AiMemorySummary,
  AiSourcePayload,
  AiSourcesResponse,
  BacktestLatest,
  BacktestObservationPage,
  BacktestRunPayload,
  BacktestStartAck,
  BacktestStatus,
  BoardState,
  ClusterAssignmentOut,
  ClusterOverviewOut,
  CompareResponse,
  DailyRankHistoryResponse,
  DailyRankPrepareAck,
  DailyRankRunnerStatus,
  DailyRankState,
  DataJobAck,
  DataJobState,
  DataStatus,
  ExportParams,
  Health,
  IntradayWatchAck,
  IntradayWatchState,
  KlineResponse,
  LimitUpsResponse,
  MatchSummaryPage,
  PatternDetailResponse,
  PatternOverview,
  PickHistoryResponse,
  PicksCheckResponse,
  PicksListResponse,
  PicksSummary,
  PoolResponse,
  PredictionPool,
  QualityErrorPage,
  QualityRunResult,
  QualitySummary,
  RealtimeStatus,
  RealtimeTop,
  ScanResponse,
  Settings,
  SettingsUpdate,
  SimilarityResponse,
  SingleState,
  SkipPage,
  StartAck,
  StoredMatchesResponse,
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

  aiSources: () => request<AiSourcesResponse>('/api/ai-sources'),

  createAiSource: (body: AiSourcePayload) =>
    request<AiSourcesResponse>('/api/ai-sources', { method: 'POST', body: JSON.stringify(body) }),

  updateAiSource: (id: string, body: AiSourcePayload) =>
    request<AiSourcesResponse>(`/api/ai-sources/${id}`, { method: 'PUT', body: JSON.stringify(body) }),

  deleteAiSource: (id: string) =>
    request<AiSourcesResponse>(`/api/ai-sources/${id}`, { method: 'DELETE' }),

  activateAiSource: (id: string) =>
    request<AiSourcesResponse>(`/api/ai-sources/${id}/active`, { method: 'PUT' }),

  aiMemorySummary: () => request<AiMemorySummary>('/api/ai-memory/summary'),

  aiMemoryDays: () => request<AiMemoryDays>('/api/ai-memory/days'),

  runAiMemory: (date?: string) =>
    request<AiMemoryRunResult>(`/api/ai-memory/run${query({ date })}`, { method: 'POST' }),

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

  intradayWatch: () => request<IntradayWatchState>('/api/realtime/intraday-watch'),

  ackIntradayWatch: (stockCode?: string, tier?: string) =>
    request<IntradayWatchAck>(
      `/api/realtime/intraday-watch/ack${query({ stock_code: stockCode, tier })}`,
      { method: 'POST' },
    ),

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

  startBacktest: (params: { start?: string; end?: string; max_days?: number } = {}) =>
    request<BacktestStartAck>(
      `/api/backtest/run${query({ start: params.start, end: params.end, max_days: params.max_days })}`,
      { method: 'POST' },
    ),

  backtestStatus: () => request<BacktestStatus>('/api/backtest/status'),

  backtestLatest: () => request<BacktestLatest>('/api/backtest/latest'),

  backtestRun: (runId: number) => request<BacktestRunPayload>(`/api/backtest/runs/${runId}`),

  backtestObservations: (params: {
    run_id: number
    band?: string
    code?: string
    limit?: number
    offset?: number
  }) =>
    request<BacktestObservationPage>(
      `/api/backtest/observations${query({
        run_id: params.run_id,
        band: params.band,
        code: params.code,
        limit: params.limit ?? 100,
        offset: params.offset ?? 0,
      })}`,
    ),

  deleteBacktest: (runId: number) =>
    request<{ ok: boolean; run_id?: number; deleted?: number }>(`/api/backtest/runs/${runId}`, {
      method: 'DELETE',
    }),

  predictionPool: (params: { date?: string; size?: number } = {}) =>
    request<PredictionPool>(`/api/prediction/pool${query({ date: params.date, size: params.size })}`),

  dataStatus: () => request<DataStatus>('/api/data/status'),

  dataJob: () => request<DataJobState>('/api/data/job'),

  rebuildFeatures: (params: { start?: string; end?: string; codes?: string } = {}) =>
    request<DataJobAck>(`/api/data/rebuild-features${query(params)}`, { method: 'POST' }),

  rebuildPatterns: (params: { start?: string; end?: string } = {}) =>
    request<DataJobAck>(`/api/data/rebuild-patterns${query(params)}`, { method: 'POST' }),

  scanMarket: (params: { start?: string; end?: string } = {}) =>
    request<DataJobAck>(`/api/data/scan-market${query(params)}`, { method: 'POST' }),

  buildTraining: (params: { start?: string; end?: string; codes?: string } = {}) =>
    request<DataJobAck>(`/api/data/build-training${query(params)}`, { method: 'POST' }),

  dataSkips: (params: { stage?: string; reason?: string; date?: string; limit?: number; offset?: number } = {}) =>
    request<SkipPage>(
      `/api/data/skips${query({
        stage: params.stage,
        reason: params.reason,
        date: params.date,
        limit: params.limit ?? 200,
        offset: params.offset ?? 0,
      })}`,
    ),

  qualitySummary: () => request<QualitySummary>('/api/quality/summary'),

  runQualityCheck: (params: { start?: string; end?: string; max_days?: number } = {}) =>
    request<QualityRunResult>(`/api/quality/check${query(params)}`, { method: 'POST' }),

  qualityErrors: (params: { date?: string; severity?: string; check?: string; limit?: number } = {}) =>
    request<QualityErrorPage>(
      `/api/quality/errors${query({
        date: params.date,
        severity: params.severity,
        check: params.check,
        limit: params.limit ?? 200,
      })}`,
    ),

  patternsOverview: (date?: string) =>
    request<PatternOverview>(`/api/patterns/overview${query({ date })}`),

  patternDetail: (code: string, withDetails = true) =>
    request<PatternDetailResponse>(`/api/patterns/${code}${query({ with_details: withDetails ? 'true' : undefined })}`),

  matchesSummary: (params: {
    date?: string
    limit?: number
    offset?: number
    q?: string
    board?: string
    sort?: string
    order?: string
  } = {}) =>
    request<MatchSummaryPage>(
      `/api/matches/summary${query({
        date: params.date,
        limit: params.limit ?? 100,
        offset: params.offset ?? 0,
        q: params.q,
        board: params.board,
        sort: params.sort,
        order: params.order,
      })}`,
    ),

  storedMatches: (code: string, date?: string) =>
    request<StoredMatchesResponse>(`/api/matches/${code}${query({ date })}`),

  recomputeMatches: (code: string, date?: string) =>
    request<StoredMatchesResponse>(`/api/matches/${code}${query({ date })}`, { method: 'POST' }),

  compareMatch: (params: { code: string; sample_code: string; date?: string; sample_date: string }) =>
    request<CompareResponse>(
      `/api/matches/compare${query({
        code: params.code,
        sample_code: params.sample_code,
        date: params.date,
        sample_date: params.sample_date,
      })}`,
    ),

  clustersOverview: (params: { date?: string; k?: number; includeMembers?: boolean } = {}) =>
    request<ClusterOverviewOut>(
      `/api/clusters/overview${query({
        date: params.date,
        k: params.k,
        include_members: params.includeMembers === false ? 'false' : undefined,
      })}`,
    ),

  clusterAssignment: (code: string, params: { date?: string; k?: number; topMembers?: number } = {}) =>
    request<ClusterAssignmentOut>(
      `/api/clusters/${code}${query({
        date: params.date,
        k: params.k,
        top_members: params.topMembers,
      })}`,
    ),

  exportMatchesCsv: async (params: { date?: string; code?: string; top?: number } = {}) => {
    const response = await fetch(
      `/api/matches/export${query({ date: params.date, code: params.code, top: params.top })}`,
    )
    const blob = await response.blob()
    if (!response.ok) {
      const raw = await blob.text()
      let detail = plainDetail(response.status, raw)
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
      `matches-${params.date ?? 'unknown'}${params.code ? `-${params.code}` : ''}.csv`
    return {
      filename,
      text,
      blob,
      rows: Number(response.headers.get('X-Export-Rows') ?? 0),
      truncated: response.headers.get('X-Export-Truncated') === '1',
    }
  },

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
      let detail = plainDetail(response.status, raw)
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
