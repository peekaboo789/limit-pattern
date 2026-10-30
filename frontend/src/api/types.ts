/** 与并行开发契约 v1 对齐的响应字段。不要在这里发明后端没有的必填项。 */

export interface Health {
  status: string
  version: string
  data_date: string | null
  data_source?: string
  active_source?: string
  exports_dir?: string
}

export interface Settings {
  retention_trading_days: number
  compare_window_trading_days: number
  ai_enabled: boolean
  ai_base_url: string
  ai_model: string
  ai_api_key_set: boolean
  data_source: string
  data_source_options?: string[]
  active_source?: string
  excluded_boards?: string[]
  included_boards?: string[]
  market_scope?: string
  last_sync_at: string | null
  last_cleanup_at: string | null
}

export interface SettingsUpdate {
  retention_trading_days?: number
  compare_window_trading_days?: number
  ai_enabled?: boolean
  ai_base_url?: string
  ai_model?: string
  data_source?: string
  ai_api_key?: string
}

export interface SyncTask {
  task_id: string
}

export interface SyncStatus {
  running: boolean
  progress: number
  message: string
  last_sync_at: string | null
  phase?: string
  done?: number
  total?: number
  saved?: number
  failed?: number
  data_source?: string
  active_source?: string
}

export interface LimitUpItem {
  code: string
  name: string
  board: 'SH' | 'SZ' | string
  close: number
  pct_chg: number
  volume: number
  amount: number
  amount_estimated?: boolean
  turnover_rate: number
  consecutive_boards: number
  first_limit_time: string | null
  limit_type: string
}

export interface LimitUpsResponse {
  date: string
  items: LimitUpItem[]
}

export interface PoolItem extends LimitUpItem {
  limit_date: string
}

export interface PoolResponse {
  dates: string[]
  items: PoolItem[]
}

export interface KBar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
  amount_estimated?: boolean
  pct_chg: number
}

export interface KlineResponse {
  code: string
  name: string
  bars: KBar[]
}

export interface SimilarityMatch {
  code: string
  name: string
  limit_date: string
  score: number
  shape_score: number
  volume_score: number
  volume_pct_score: number
  other_scores: Record<string, number>
  window: string[]
}

export interface SimilarityResponse {
  target: {
    code: string
    name: string
    window: string[]
  }
  pool_dates: string[]
  pool_size: number
  disclaimer: string
  matches: SimilarityMatch[]
}

export interface ScanBestMatch {
  code: string
  name: string
  limit_date: string
}

export interface ScanItem {
  code: string
  name: string
  score: number
  best_match: ScanBestMatch
  shape_score: number
  volume_score: number
  volume_pct_score: number
}

export interface ScanResponse {
  date: string
  pool_size: number
  disclaimer: string
  items: ScanItem[]
}

export interface SummaryListItem {
  date: string
  limitup_count: number
  headline: string
}

export interface SummaryListResponse {
  items: SummaryListItem[]
}

/**
 * 每日总结至少包含契约列出的字段。
 * 嵌套结构在 docs/summary_schema.md 落地前按宽松形态解析，见 utils/summary.ts。
 */
export type ExportType = 'scan' | 'similarity' | 'pool' | 'summary' | 'ai_ranking'

export interface ExportParams {
  type: ExportType
  date: string
  code?: string
  top?: number
}

export interface SummaryDocument {
  date: string
  limitup_count: number
  sectors: unknown[]
  ladder: unknown[]
  patterns: unknown[]
  volume_features: Record<string, number | string | null>
  key_points: unknown[]
  model: string
  generated_at: string
  headline?: string
}

export interface AiRankingItem {
  rank: number
  code: string
  name: string
  board_name: string
  ai_score: number
  reason: string
  similarity: number
  shape_score: number
  volume_score: number
  volume_pct_score: number
  best_match: { code: string; name: string; limit_date: string }
  close: number | null
}

export interface AiRanking {
  date: string
  run_at: string
  trigger: string
  model: string
  candidate_count: number
  pool_dates: string[]
  pool_size: number
  disclaimer: string
  items: AiRankingItem[]
  runs?: string[]
}

export type MonitorLevel = 'normal' | 'warn' | 'alert'

export interface MonitorItem {
  rank: number | null
  code: string
  name: string
  ai_score: number | null
  reason: string
  price: number | null
  pre_close: number | null
  pct: number | null
  level: MonitorLevel
}

export interface MonitorSnapshot {
  date: string
  market_open: boolean
  status_text: string
  ranking_date: string | null
  thresholds: { warn: number; alert: number }
  source: string
  error: string
  updated_at: string
  items: MonitorItem[]
  alert_count?: number
  warn_count?: number
}

export interface MonitorEvent {
  id: number
  code: string
  name: string
  level: 'warn' | 'alert'
  pct: number | null
  rank: number | null
  at: string | null
}

export interface MonitorEvents {
  date: string
  market_open: boolean
  last_id: number
  items: MonitorEvent[]
}
