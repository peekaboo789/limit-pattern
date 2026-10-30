/** 与并行开发契约 v1 对齐的响应字段。不要在这里发明后端没有的必填项。 */

export interface Health {
  status: string
  version: string
  data_date: string | null
  data_source?: string
  active_source?: string
  exports_dir?: string
  stored_days?: number
  sample_pool_days?: number
  retention_days?: number
  earliest_date?: string | null
}

export interface Settings {
  retention_trading_days: number
  compare_window_trading_days: number
  sample_pool_trading_days?: number
  retention_min_trading_days?: number
  feature_window_trading_days?: number
  ai_enabled: boolean
  ai_base_url: string
  ai_model: string
  ai_api_key_set: boolean
  ai_thinking?: string
  ai_timeout_seconds?: number
  alert_trading_hours_only?: boolean
  alert_include_auction?: boolean
  ai_batch_size?: number
  ai_parallel?: number
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
  sample_pool_trading_days?: number
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
  current_source?: string
  errors?: string[]
  sources?: Record<string, { failures: number; paused: boolean }>
  cancelling?: boolean
}

export interface SourceTestResult {
  source: string
  ok: boolean
  latency_ms: number
  bars?: number
  latest?: string
  error: string
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
  counts?: Record<string, number>
  latest?: string | null
  empty_reason?: string
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
export type ExportType = 'scan' | 'similarity' | 'pool' | 'summary' | 'ai_ranking' | 'next_day_pool' | 'backtest' | 'cluster'

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
  similarity: number | null
  shape_score: number | null
  volume_score: number | null
  volume_pct_score: number | null
  best_match: { code: string; name: string; limit_date: string } | null
  close: number | null
  sub?: Record<string, number | null>
  risk?: string
  risk_deduct?: number
  tags?: string[]
  checks?: { claim: string; fact: string }[]
}

export interface AiRanking {
  date: string
  run_at: string
  trigger: string
  model: string
  candidate_count: number
  pool_dates: string[]
  pool_size: number
  pool_days?: number
  batches?: { total: number; failed: number; split: number; batch_size: number }
  unscored_count?: number
  imported_count?: number
  check_count?: number
  outside_candidates?: string[]
  dropped_unknown?: string[]
  skipped_count?: number
  limitup_count?: number
  disclaimer: string
  items: AiRankingItem[]
  runs?: string[]
  market?: MarketContext
  dimensions?: { key: string; label: string; weight: number }[]
  snapshot?: { kind: 'intraday' | 'morning_freeze' | 'close' | 'daily'; taken_at: string | null; count: number; volume_estimated?: boolean; volume_fraction?: number; elapsed_minutes?: number }
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
  phase?: string
  alerts_active?: boolean
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
  phase?: string
  alerts_active?: boolean
  status_text?: string
  last_id: number
  items: MonitorEvent[]
}

export interface BandStat {
  band: string
  count: number
  limit_up: number
  limit_up_rate: number | null
  up3_rate: number | null
  avg_next_pct: number | null
}

export interface BacktestDay extends BandStat {
  date: string
  next_date: string
  hits?: string[]
}

export interface SimilarityBacktest {
  kind: 'similarity'
  top: number
  disclaimer: string
  days: BacktestDay[]
  bands: BandStat[]
}

export interface RankingBacktest {
  kind: 'ai_ranking'
  disclaimer: string
  days: BacktestDay[]
  total: BandStat
}

export interface NextDayPoolItem {
  code: string
  name: string
  score: number
  band: string
  band_samples: number
  band_limit_up_rate: number | null
  band_up3_rate: number | null
  best_match: { code: string; name: string; limit_date: string }
}

export interface NextDayPool {
  date: string | null
  pool_dates?: string[]
  disclaimer: string
  bands: BandStat[]
  items: NextDayPoolItem[]
}

export interface CalendarInfo {
  year: number
  estimated: boolean
  holidays: string[]
  today: string
  today_is_trading_day: boolean
  market_open: boolean
  previous_trading_day: string
  next_trading_day: string
}

export interface ClusterPatternRow {
  day: string
  close_change: number | null
  volume_ratio_5: number | null
  amplitude: number | null
}

export interface ClusterGroup {
  id: number
  name: string
  size: number
  members: { code: string; name: string; limit_date: string }[]
  pattern: ClusterPatternRow[]
  return_3d: number | null
  ai_text?: string
}

export interface ClusterResult {
  date: string
  pool_dates: string[]
  method: string
  requested_k: number | string
  sample_count: number
  k?: number
  silhouette?: number | null
  disclaimer: string
  generated_at: string
  note?: string
  ai_note?: string
  clusters: ClusterGroup[]
  points: { code: string; name: string; cluster: number; x: number; y: number }[]
}

export interface MarketContext {
  date: string
  stocks: number
  up: number
  down: number
  flat?: number
  suspended?: number
  listed?: number
  up_down_ratio: number | null
  median_pct: number | null
  limit_up: number
  limit_down: number
  broken: number
  broken_rate: number | null
  max_height: number
  leaders: string[]
}
