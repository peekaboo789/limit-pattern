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
export type ExportType = 'scan' | 'similarity' | 'pool' | 'summary'

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

// --- 盘中实时监控 -----------------------------------------------------------

export interface MetricOption {
  key: string
  label: string
}

export interface RealtimeBestMatch {
  code: string
  name: string
  limit_date: string
  score: number | null
}

export interface RealtimeRow {
  code: string
  name: string
  exchange: string
  board: string
  board_label: string
  latest: number | null
  pct_chg: number | null
  pre_close: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  turnover_rate: number | null
  limit_percent: number | null
  limit_up_price: number | null
  to_limit_percent: number | null
  is_limit_up: boolean
  limit_confirmed: boolean
  score: number | null
  shape_score: number | null
  volume_score: number | null
  volume_pct_score: number | null
  avg_score: number | null
  high_score_count: number
  sample_count: number
  band: string
  /** 概率列：这一行落的档、该档在最近一次已完成回测里的次日发生率与观测数。 */
  similarity_band: string
  next_day_rate_percent: number | null
  next_day_rate_samples: number
  best_match: RealtimeBestMatch | null
}

/** `scan()` 顶层去掉 `rows`，再由榜本身补上两榜与条数。 */
export interface RealtimeSnapshot {
  trade_date: string
  base_date: string
  pool_dates: string[]
  pool_size: number
  generated_at: string
  source: string
  provider: string
  phase: string
  in_session: boolean
  window_mode: string
  morning_captured_at: string | null
  morning_bar_count: number
  morning_freeze: Record<string, unknown> | null
  skipped_count: number
  skipped_samples: unknown[]
  limit_up_count: number
  pending_count: number
  note: string
  disclaimer: string
  /** 这一轮榜的概率列底表：整轮只读一份，所有行来自同一次回测。 */
  probability?: ProbabilityTable
  metric?: string
  metric_label?: string
  top_n?: number
  limit_up_rows: RealtimeRow[]
  pending_rows: RealtimeRow[]
  row_count: number
}

export interface RealtimeTop {
  status: 'warming' | 'live' | 'unavailable' | string
  top_n: number
  top_n_default: number
  top_n_min: number
  top_n_max: number
  refresh_seconds: number
  full_round_seconds: number
  metric: string
  metric_label: string
  metrics: MetricOption[]
  leader_count: number
  full_rounds: number
  fast_rounds: number
  full_round_at: string | null
  full_round_seconds_ago: number | null
  fast_round_at: string | null
  fast_round_seconds_ago: number | null
  market: {
    scanned: number
    limit_up_count: number
    pending_count: number
    skipped_count: number
  } | null
  error: string
  snapshot: RealtimeSnapshot | null
  row_count?: number
}

export interface RealtimeStatus {
  status: string
  top_n: number
  top_n_default: number
  top_n_min: number
  top_n_max: number
  refresh_seconds: number
  full_round_seconds: number
  idle_exit_seconds: number
  metric: string
  metrics: MetricOption[]
  tracker_running: boolean
  leader_count: number
  full_rounds: number
  fast_rounds: number
  full_rows: number
  phase: string
  in_session: boolean
  source: string
  window_mode: string
  morning_freeze: Record<string, unknown> | null
  error: string
  trade_date: string
  base_date: string
  pool_dates: string[]
  pool_size: number
  note: string
  disclaimer: string
}

// --- 盘中涨幅报警榜 ----------------------------------------------------------

/** 一条报警：一天、一只、一档只有这一条，`acked_at` 空串就是还没点「知道了」。 */
export interface WatchAlert {
  id: number
  trade_date: string
  stock_code: string
  stock_name: string
  tier: string
  tier_label: string
  rank: number | null
  score: number | null
  percent: number | null
  rise_3min: number | null
  fired_at: string
  acked_at: string
}

export interface WatchRow {
  stock_code: string
  stock_name: string
  rank: number | null
  score: number | null
  reason: string
  counter_evidence: string
  feed_rank: number | null
  last_price: number | null
  change_percent: number | null
  rise_3min: number | null
  sample_span_seconds: number | null
  sampled_at: string
  no_snapshot: boolean
  alert: WatchAlert | null
  spike: WatchAlert | null
}

export interface WatchSampling {
  rounds: number
  last_round_at: string | null
  last_round_seconds_ago: number | null
  snapshot_date: string
  today: string
  source: string
  error: string
  thread_running: boolean
}

export interface WatchPoolInfo {
  pool_date: string | null
  status: string
  note: string
  row_count: number
}

export interface IntradayWatchState {
  status: string
  trade_date: string
  session_phase: string
  note: string
  pool: WatchPoolInfo | null
  rows: WatchRow[]
  row_count: number
  alerts: WatchAlert[]
  unacked: WatchAlert[]
  pending_pin_codes: string[]
  pinning: boolean
  sampling: WatchSampling
  pool_size: number
  alert_tier: string
  pin_tier: string
  alert_percent: number
  pin_percent: number
  rise_window_seconds: number
  rise_min_points: number
  poll_seconds: number
  sample_keep_seconds: number
  idle_exit_seconds: number
  disclaimer: string
}

/** 点「知道了」的回包：还剩几条、窗口该不该继续置顶，都是库里刚读完的那一份。 */
export interface IntradayWatchAck {
  trade_date: string
  stock_code: string | null
  tier: string | null
  ok: boolean
  acked?: number
  remaining?: number
  pinning?: boolean
  pending_pin_codes?: string[]
  error?: string
}

// --- AI 分析（整榜综合分析、单股直评）---------------------------------------

export interface FormulaBlock {
  composite_score: number
  weighted: number
  risk_penalty: number
  level: string
  contributions: Record<string, number | null>
  missing_items: string[]
  weight_total: number
  has_signal: boolean
  model_gap?: number | null
  model_score?: number | null
}

export interface IndicatorScores {
  similarity_score: number | null
  pattern_match_rate: number | null
  technical_score: number | null
  volume_score: number | null
  capital_score: number | null
  history_limit_up_rate: number | null
  risk_score: number | null
  [key: string]: number | null
}

export interface EvidenceRow {
  stock_code: string
  stock_name: string
  board: string
  at_limit: boolean
  limit_confirmed: boolean
  last_price: number | null
  change_percent: number | null
  to_limit_percent: number | null
  limit_up_price: number | null
  max_similarity: number | null
  avg_similarity: number | null
  high_sim_count: number | null
  sample_count: number | null
  band: string
  best_match: RealtimeBestMatch | null
  ai_history: AiHistory | null
  indicator_scores: IndicatorScores
  formula: FormulaBlock
  feed_rank: number
}

export interface AiPick {
  stock_code: string
  stock_name: string
  score: number | null
  reason: string
  counter_evidence: string
  feed_rank: number | null
  evidence: EvidenceRow
}

export interface AiUsage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  [key: string]: unknown
}

export interface AiReport {
  ok: boolean
  status: string
  chain: string
  request_sent: boolean
  /** 这一趟按 token 计了费没有。上游 429/5xx 那种「做出结论之前就挡回来」是 false。 */
  billed?: boolean
  model: string
  error: string
  started_at: string
  finished_at: string
  latency_seconds: number
  picks: AiPick[]
  picks_beyond_limit: number
  dropped_codes: string[]
  duplicate_codes: string[]
  text_truncated: boolean
  criteria: string
  criteria_truncated: boolean
  usage: AiUsage
  cost_text: string
  response_format?: boolean
  raw_excerpt: string
  board_rows: number
  feed_rows: number
  skipped_rows: number
  history_missing_rows: number
  feature_missing_rows: number
  board_generated_at?: string
  formula: string
  disclaimer: string
  report_id: number | null
  store_error: string
  picks_recorded?: number
  trade_date?: string
  base_date?: string
  window_mode?: string
  metric?: string
  metric_label?: string
  top_n?: number
  source?: string
  pool_size?: number
  code?: string
  key?: string
  pick_history?: Record<string, AiHistory>
  ai_history?: AiHistory | null
}

/** `runner.status()`：没在跑时只有 `running` 与 `chain`。 */
export interface RunnerStatus {
  running: boolean
  chain?: string
  started_at?: string
  answer_chars?: number
  thinking_chars?: number
  running_seconds?: number
  key?: string
  metric?: string
  metric_label?: string
  top_n?: number
  code?: string
  label?: string
  accepted?: boolean
  already_running?: boolean
}

export interface StartAck {
  accepted: boolean
  already_running: boolean
  label?: string
  started_at?: string
  key?: string
}

export interface AiReportListItem {
  report_id: number
  trade_date: string | null
  chain: string
  status: string
  model: string
  metric: string
  metric_label: string
  window_mode: string
  started_at: string
  finished_at: string
  latency_seconds: number | null
  board_rows: number
  feed_rows: number
  picks_count: number
  recommended_codes: string[]
  cost_text: string
  error: string
}

export interface AiReportListResponse {
  items: AiReportListItem[]
  retention_days: number
  max_reports: number
}

export interface BoardState {
  runner: RunnerStatus
  source: string
  report: AiReport | null
}

export interface SingleState {
  code: string
  runner: RunnerStatus
  source: string
  report: AiReport | null
  ai_history: AiHistory | null
}

// --- AI 推荐的对账账本（零成本）---------------------------------------------

export interface AiHistory {
  picks: number
  hits: number
  misses: number
  pending: number
  settled: number
  hit_rate_percent: number | null
  avg_score: number | null
  all_pending: boolean
  history_score: number | null
  formula?: string
  recent?: AiPickRecord[]
}

export interface AiPickRecord {
  id: number
  report_id: number | null
  trade_date: string | null
  judge_date: string | null
  recommended_at: string
  stock_code: string
  stock_name: string
  source: string
  model: string
  score: number | null
  criteria: string
  reason: string
  counter_evidence: string
  verdict: string
  checked_at: string | null
  at: {
    max_similarity: number | null
    composite_score: number | null
    level: string
    model_gap: number | null
    to_limit_percent: number | null
    change_percent: number | null
  }
  actual: {
    is_limit_up: boolean | null
    close: number | null
    change_percent: number | null
    max_change_percent: number | null
    source: string
  }
}

export interface PickHistoryResponse {
  code: string
  never_recommended: boolean
  history: AiHistory | null
}

export interface PicksSummary {
  check: {
    total: number
    hits: number
    misses: number
    pending: number
    settled: number
    hit_rate_percent: number | null
    last_checked_at: string | null
  }
  days: { judge_date: string; rows: number; hits: number; misses: number; pending: number; hit_rate_percent: number | null }[]
  verdict_rule: string
  formula: string
  disclaimer: string
}

export interface PicksCheckResponse extends PicksSummary {
  reconcile: {
    judged: number
    hits: number
    misses: number
    still_pending: number
    checked_at: string
  }
}

export interface PicksListResponse {
  items: AiPickRecord[]
  limit: number
  total_read: number
}

// --- 每日 AI 固定榜 ----------------------------------------------------------

export interface DailyRankPick {
  stock_code: string
  stock_name: string
  score: number
  rank: number
  reason: string
  counter_evidence: string
  feed_rank: number | null
  formula: FormulaBlock
  indicator_scores: IndicatorScores
  evidence: EvidenceRow
  unverified_citation: boolean
}

export interface DailyRankFeedRow {
  feed_rank: number
  stock_code: string
  stock_name: string
  board: string
  at_limit: boolean
  last_price: number | null
  change_percent: number | null
  to_limit_percent: number | null
  max_similarity: number | null
  avg_similarity: number | null
  high_sim_count: number | null
  sample_count: number | null
  band: string
}

export interface DailyRankResult {
  /** 落库那一份也带上钱这两笔：重启之后还能说清上一趟花没花。 */
  request_sent?: boolean
  billed?: boolean
  criteria: string
  criteria_truncated?: boolean
  text_truncated?: boolean
  picks: DailyRankPick[]
  picks_count: number
  beyond_limit: number
  dropped_codes: string[]
  dropped_codes_count: number
  duplicate_codes?: string[]
  rejected_codes: string[]
  rejected_count: number
  unverified_codes: string[]
  unverified_citations: number
  usage?: AiUsage
  cost_text: string
  response_format?: boolean
  raw_excerpt: string
  feed_rows: number
  board_rows: number
  formula?: string
  disclaimer: string
}

export interface DailyRankBoard {
  row_id: number
  trade_date: string | null
  base_date: string | null
  status: string
  ok: boolean
  model: string
  prepared_at: string
  started_at: string
  finished_at: string
  latency_seconds: number | null
  board_rows: number
  feed_rows: number
  window_mode: string
  metric: string
  metric_label: string
  picks_count: number
  picks_beyond_limit: number
  dropped_codes_count: number
  rejected_count: number
  unverified_citations: number
  cost_text: string
  error: string
  result: DailyRankResult | null
  feed: DailyRankFeedRow[]
  extra: Record<string, unknown>
  formula: string
  disclaimer: string
}

export interface DailyRankState {
  board: DailyRankBoard | null
  asked_date: string
  runner: DailyRankRunnerStatus
}

export interface DailyRankRunnerStatus {
  running: boolean
  last?: {
    trade_date?: string
    finished_at?: string
    ok?: boolean
    request_sent?: boolean
    billed?: boolean
  } | null
  trade_date?: string
  started_at?: string
  answer_chars?: number
  thinking_chars?: number
  running_seconds?: number
}

export interface DailyRankPrepareAck {
  ok: boolean
  status: string
  trade_date: string
  board_rows: number
  feed_rows: number
  prepared_at?: string
  error: string
}

export interface DailyRankHistoryItem {
  trade_date: string | null
  status: string
  metric_label: string
  model: string
  prepared_at: string
  finished_at: string
  latency_seconds: number | null
  feed_rows: number
  picks_count: number
  rejected_count: number
  unverified_citations: number
  cost_text: string
  error: string
}

export interface DailyRankHistoryResponse {
  items: DailyRankHistoryItem[]
  retention_days: number
  feed_limit: number
  pick_count: number
}

// --- 档2：回测、概率列与次日预测池 ------------------------------------------

/** 概率列底表的一档。`percent` 为 null = 这一档给不出（观测不足 / 没出现过），不是 0%。 */
export interface ProbabilityRate {
  percent: number | null
  samples: number
  lift: number | null
}

/** 最近一次**已完成**回测的分档发生率。`run_id` 为 null = 还没跑过回测。 */
export interface ProbabilityTable {
  rates: Record<string, ProbabilityRate>
  floors: number[]
  run_id: number | null
  start_date: string | null
  end_date: string | null
  day_count: number
  observation_count: number
  baseline_percent: number | null
  min_band_observations: number
  note: string
}

export interface BacktestSpotCheck {
  stock_code: string
  trade_date: string
  recorded: number | null
  recomputed: number | null
  backtest_pool: number | null
  production_pool: number | null
  compared: boolean
  ok: boolean | null
  reason: string
}

export interface BacktestLeakage {
  passed: boolean
  window_upto_day?: boolean
  window_excludes_label?: boolean
  label_after_day?: boolean
  sample_window_before_limit?: boolean
  spot_checks: BacktestSpotCheck[]
  detail: string
  days: number
}

export interface CalibrationBin {
  lower: number
  upper: number
  mean_predicted: number
  observed_rate: number
  count: number
}

export interface CalibrationBandCheck {
  band: string
  predicted: number
  observed: number
  count: number
}

export interface BacktestCalibration {
  available: boolean
  reason: string
  split_ratio: number
  train_days: number
  test_days: number
  train_count: number
  test_count: number
  brier: number | null
  log_loss: number | null
  ece: number | null
  bins: CalibrationBin[]
  band_probabilities: Record<string, number>
  band_checks: CalibrationBandCheck[]
}

export interface BacktestBucket {
  band: string
  sample_count: number
  limit_up_count: number
  limit_up_rate: number | null
  baseline_rate: number | null
  lift: number | null
  pending_count: number
}

export interface BacktestDay {
  trade_date: string
  limit_up_count: number
  pool_size: number
  pool_dates: string[]
  skipped_samples: number
  universe_count: number
  scanned_count: number
  missing_bars: number
  dirty_skipped: number
  sample_window_ok: boolean
  next_trade_date: string | null
}

export interface BacktestRunPayload {
  run_id: number
  start_date: string
  end_date: string
  status: string
  note: string
  error: string
  split_ratio: number
  day_count: number
  observation_count: number
  baseline_rate: number | null
  created_at: string | null
  finished_at: string | null
  parameters: Record<string, unknown>
  days: BacktestDay[]
  buckets: BacktestBucket[]
  calibration: BacktestCalibration
  leakage: BacktestLeakage
  disclaimer: string
}

export interface BacktestRunSummary {
  run_id: number
  start_date: string
  end_date: string
  status: string
  note: string
  error: string
  day_count: number
  observation_count: number
  baseline_rate: number | null
  split_ratio: number
  created_at: string | null
  finished_at: string | null
}

export interface BacktestLatest {
  run: BacktestRunPayload | null
  probability: ProbabilityTable
  runs: BacktestRunSummary[]
  disclaimer: string
}

export interface BacktestStatus {
  running: boolean
  progress: number
  phase: string
  message: string
  task_id: string
  days_done: number
  days_total: number
  current_date: string
  error: string
  run_id: number | null
  last_finished_at: string
}

export interface BacktestStartAck {
  task_id: string
  already_running?: boolean
  running: boolean
  progress: number
  message: string
  run_id?: number | null
}

export interface BacktestObservation {
  trade_date: string
  stock_code: string
  stock_name: string
  max_similarity: number | null
  avg_similarity: number | null
  sample_count: number
  high_similarity_count: number
  band: string
  next_date: string | null
  next_limit_up: boolean | null
  next_change_percent: number | null
}

export interface BacktestObservationPage {
  run_id: number
  band: string
  stock_code: string
  total: number
  limit: number
  offset: number
  items: BacktestObservation[]
}

export interface PredictionScoreTerms {
  shape: number
  carry: number | null
  band: number | null
}

export interface PredictionRow {
  stock_code: string
  stock_name: string
  board: string
  board_label: string
  is_st: boolean
  pool_rank: number
  rank: number
  max_similarity: number | null
  avg_similarity: number | null
  sample_count: number
  high_similarity_count: number
  high_similarity_rate: number | null
  band: string
  band_rate: number | null
  band_samples: number
  band_lift: number | null
  carry_known_count: number
  carry_hit_count: number
  carry_missing_count: number
  carry_rate: number | null
  close: number | null
  change_percent: number | null
  volume: number | null
  turnover_rate: number | null
  prediction_score: number
  score_terms: PredictionScoreTerms
}

export interface PredictionBand {
  band: string
  sample_count: number
  limit_up_count: number
  limit_up_rate: number | null
  baseline_rate: number | null
  lift: number | null
  pending_count: number
}

export interface PredictionPool {
  pool_date: string
  next_trade_date: string | null
  pool_size: number
  pool_total: number
  pool_dates: string[]
  sample_pool_size: number
  skipped_samples: unknown[]
  missing_bars: number
  dirty_skipped: number
  rows: PredictionRow[]
  bands: PredictionBand[]
  baseline_rate: number | null
  calibrated: boolean
  backtest_run_id: number | null
  backtest_day_count: number
  backtest_observation_count: number
  min_band_observations: number
  note: string
  formula: string
  disclaimer: string
}
