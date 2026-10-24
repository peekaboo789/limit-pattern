<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type {
  DataJobAck,
  DataJobState,
  DataStatus,
  QualityErrorPage,
  QualityRunResult,
  QualitySummary,
  SkipPage,
} from '@/api/types'
import { formatDateTime } from '@/utils/format'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const status = ref<DataStatus | null>(null)
const quality = ref<QualitySummary | null>(null)
const skips = ref<SkipPage | null>(null)
const errors = ref<QualityErrorPage | null>(null)
const job = ref<DataJobState | null>(null)
const loading = ref(false)
const error = ref('')
const actionError = ref('')
const notice = ref('')
const checking = ref(false)

const startDay = ref('')
const endDay = ref('')
const codesInput = ref('')
const skipStage = ref('all')
const skipReason = ref('all')
const errorSeverity = ref('error')
const errorCheck = ref('all')
let timer: number | null = null

const counts = computed(() => status.value?.counts ?? null)
const busy = computed(() => status.value?.running_job !== '' || job.value?.running === true)
const runningLabel = computed(() => status.value?.running_job || '')
const progress = computed(() => job.value?.progress ?? 0)
const result = computed(() => job.value?.result ?? null)
const resultDays = computed(() => result.value?.days?.length ?? 0)
const finished = computed(() => job.value && !job.value.running && job.value.kind)

const coverage = computed(() => {
  const data = status.value
  if (!data) return []
  const days = new Set<string>([
    ...Object.keys(data.features_by_date),
    ...Object.keys(data.patterns_by_date),
    ...Object.keys(data.matches_by_date),
    ...Object.keys(data.training_by_date),
  ])
  return [...days].sort().reverse().map((day) => ({
    day,
    features: data.features_by_date[day] ?? 0,
    patterns: data.patterns_by_date[day] ?? 0,
    matches: data.matches_by_date[day] ?? 0,
    training: data.training_by_date[day] ?? 0,
  }))
})

const skipReasons = computed(() => Object.keys(skips.value?.by_reason ?? {}).sort())

const stageLabels: Record<string, string> = {
  feature: '特征',
  pattern: '样本',
  match: '匹配',
  training: '训练',
}

const tasks = [
  { kind: 'features', label: '重新计算特征', hint: 'daily_features' },
  { kind: 'patterns', label: '重建涨停样本', hint: 'limit_up_patterns' },
  { kind: 'scan', label: '全市场匹配扫描', hint: 'pattern_matches' },
  { kind: 'training', label: '构造训练集', hint: 'stock_training_data' },
] as const

function range() {
  return {
    start: startDay.value || undefined,
    end: endDay.value || undefined,
    codes: codesInput.value.trim() || undefined,
  }
}

async function runTask(kind: string) {
  actionError.value = ''
  notice.value = ''
  const params = range()
  try {
    let ack: DataJobAck
    if (kind === 'features') ack = await api.rebuildFeatures({ start: params.start, end: params.end, codes: params.codes })
    else if (kind === 'patterns') ack = await api.rebuildPatterns({ start: params.start, end: params.end })
    else if (kind === 'scan') ack = await api.scanMarket({ start: params.start, end: params.end })
    else ack = await api.buildTraining({ start: params.start, end: params.end, codes: params.codes })
    notice.value = ack.message
    startPolling()
  } catch (reason) {
    actionError.value = reason instanceof ApiError ? reason.detail : '任务起不来'
  }
}

async function loadQuality() {
  try {
    quality.value = await api.qualitySummary()
    errors.value = await api.qualityErrors({
      severity: errorSeverity.value,
      check: errorCheck.value === 'all' ? undefined : errorCheck.value,
      limit: 200,
    })
  } catch (reason) {
    if (reason instanceof ApiError) actionError.value = reason.detail
  }
}

async function runCheck() {
  checking.value = true
  actionError.value = ''
  try {
    const resultRun: QualityRunResult = await api.runQualityCheck({
      start: range().start,
      end: range().end,
    })
    notice.value = `检查了 ${resultRun.scanned_days} 天 · ${resultRun.scanned_rows} 行，新记 ${resultRun.written} 条，error ${resultRun.errors} / warning ${resultRun.warnings}`
    await Promise.all([loadQuality(), load()])
  } catch (reason) {
    actionError.value = reason instanceof ApiError ? reason.detail : '质量检查跑不成'
  } finally {
    checking.value = false
  }
}

async function loadSkips() {
  try {
    skips.value = await api.dataSkips({
      stage: skipStage.value === 'all' ? undefined : skipStage.value,
      reason: skipReason.value === 'all' ? undefined : skipReason.value,
      limit: 200,
    })
  } catch (reason) {
    if (reason instanceof ApiError) actionError.value = reason.detail
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    status.value = await api.dataStatus()
    job.value = status.value.job
  } catch (reason) {
    if (reason instanceof ApiError) error.value = reason.detail
  } finally {
    loading.value = false
  }
  void loadQuality()
  void loadSkips()
}

function stopPolling() {
  if (timer != null) {
    window.clearInterval(timer)
    timer = null
  }
}

function startPolling() {
  if (timer != null) return
  timer = window.setInterval(() => void tick(), 2000)
  void tick()
}

async function tick() {
  try {
    const state = await api.dataJob()
    job.value = state
    if (!state.running) {
      stopPolling()
      await load()
    }
  } catch {
    //: 读不到进度就维持上一份，别把页面顶成一条红条。
  }
}

watch([skipStage], () => void loadSkips())
watch([skipReason], () => void loadSkips())
watch([errorSeverity, errorCheck], () => void loadQuality())

onMounted(() => {
  void load().then(() => {
    if (status.value?.running_job) startPolling()
  })
})

onUnmounted(stopPolling)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>数据管理</h1>
      <p class="lede">库里各类派生数据有多少行、覆盖哪几天，以及四个重建任务。</p>
    </div>
    <div class="toolbar">
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">
        {{ loading ? '读数中…' : '刷新' }}
      </button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <p v-if="actionError" class="alert">{{ actionError }}</p>
  <p v-if="notice" class="alert alert-ok">{{ notice }}</p>

  <section v-if="status" class="stats">
    <div class="card stat">
      <div class="stat-label">行情行</div>
      <div class="stat-value">{{ counts?.daily_quotes ?? 0 }}</div>
      <div class="stat-foot">{{ status.earliest_trade_date }} ~ {{ status.latest_trade_date }} · {{ status.trade_day_count }} 天</div>
    </div>
    <div class="card stat">
      <div class="stat-label">特征行</div>
      <div class="stat-value">{{ counts?.daily_features ?? 0 }}</div>
      <div class="stat-foot">保留 {{ status.retention_trading_days }} 个交易日</div>
    </div>
    <div class="card stat">
      <div class="stat-label">涨停样本</div>
      <div class="stat-value">{{ counts?.limit_up_patterns ?? 0 }}</div>
      <div class="stat-foot">样本池 {{ status.pool_dates.length }} 天</div>
    </div>
    <div class="card stat">
      <div class="stat-label">匹配明细行</div>
      <div class="stat-value">{{ counts?.pattern_matches ?? 0 }}</div>
      <div class="stat-foot">每股前 {{ status.match_top_n }} 条 · 汇总 {{ counts?.match_summaries ?? 0 }} 行</div>
    </div>
    <div class="card stat">
      <div class="stat-label">训练行</div>
      <div class="stat-value">{{ counts?.stock_training_rows ?? 0 }}</div>
      <div class="stat-foot">闸门挡住 {{ status.training.blocked_rows }} 行</div>
    </div>
    <div class="card stat">
      <div class="stat-label">质量 error</div>
      <div class="stat-value">{{ status.quality.errors }}</div>
      <div class="stat-foot">warning {{ status.quality.warnings }} · 最晚 {{ status.quality.latest_date || '—' }}</div>
    </div>
    <div class="card stat">
      <div class="stat-label">跳过账</div>
      <div class="stat-value">{{ status.skips.total }}</div>
      <div class="stat-foot">补拉 K 线 {{ counts?.calc_bars ?? 0 }} 行</div>
    </div>
    <div class="card stat">
      <div class="stat-label">上次质量检查</div>
      <div class="stat-value" style="font-size: 15px">{{ formatDateTime(status.quality.last_quality_at) }}</div>
      <div class="stat-foot">{{ status.compare_window_days }} 日窗口比对</div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>重建任务</h2>
      <span v-if="runningLabel" class="pill tag-hot">{{ runningLabel }}</span>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>起始日</span>
        <input v-model="startDay" type="date" />
      </label>
      <label class="field">
        <span>结束日</span>
        <input v-model="endDay" type="date" />
      </label>
      <label class="field">
        <span>只跑这些代码（可选）</span>
        <input v-model="codesInput" class="mono" placeholder="600809,000001" />
      </label>
    </div>
    <div class="toolbar" style="margin-top: 10px">
      <button
        v-for="task in tasks"
        :key="task.kind"
        class="btn"
        type="button"
        :disabled="busy"
        @click="runTask(task.kind)"
      >
        {{ task.label }}
      </button>
    </div>
    <div v-if="job && (job.running || finished)" style="margin-top: 14px">
      <div class="progress" :class="{ running: job.running }">
        <i :style="{ '--w': `${progress}%` }" />
      </div>
      <p class="muted" style="margin-top: 8px">{{ job.message }}<span v-if="job.step"> · {{ job.step }}</span></p>
      <p v-if="job.error" class="alert" style="margin-top: 8px">{{ job.error }}</p>
      <div v-if="result" class="table-wrap" style="margin-top: 10px; max-height: none">
        <table>
          <tbody>
            <tr v-if="result.start"><td class="muted">区间</td><td class="mono">{{ result.start }} ~ {{ result.end }}</td></tr>
            <tr v-if="resultDays"><td class="muted">覆盖交易日</td><td>{{ resultDays }}</td></tr>
            <tr v-if="result.warmup_from"><td class="muted">预热从</td><td class="mono">{{ result.warmup_from }}</td></tr>
            <tr v-if="result.pool_size"><td class="muted">样本池</td><td>{{ result.pool_size }} 条</td></tr>
            <tr v-if="result.feature_count"><td class="muted">特征行</td><td>{{ result.feature_count }}</td></tr>
            <tr v-if="result.limit_up_count"><td class="muted">当日涨停</td><td>{{ result.limit_up_count }}</td></tr>
            <tr v-if="result.pattern_count"><td class="muted">样本行</td><td>{{ result.pattern_count }}</td></tr>
            <tr v-if="result.target_count"><td class="muted">参与打分的股票</td><td>{{ result.target_count }}</td></tr>
            <tr v-if="result.match_count"><td class="muted">匹配明细行</td><td>{{ result.match_count }}</td></tr>
            <tr v-if="result.summary_count"><td class="muted">汇总行</td><td>{{ result.summary_count }}</td></tr>
            <tr v-if="result.training_count"><td class="muted">训练行</td><td>{{ result.training_count }}</td></tr>
            <tr v-if="result.training_blocked"><td class="muted">被闸门挡住</td><td>{{ result.training_blocked }}</td></tr>
            <tr v-if="result.skipped_total"><td class="muted">跳过合计</td><td>{{ result.skipped_total }}</td></tr>
            <tr v-for="(count, reason) in result.skipped ?? {}" :key="reason">
              <td class="muted">跳过 · {{ reason }}</td>
              <td>{{ count }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>每日覆盖</h2></div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>交易日</th>
            <th class="right">特征行</th>
            <th class="right">涨停样本</th>
            <th class="right">匹配汇总</th>
            <th class="right">训练行</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in coverage" :key="row.day">
            <td class="mono">{{ row.day }}</td>
            <td class="right">{{ row.features }}</td>
            <td class="right">{{ row.patterns }}</td>
            <td class="right">{{ row.matches }}</td>
            <td class="right">{{ row.training }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!coverage.length" class="empty">库里还没有可数的派生数据</p>
    </div>
  </section>

  <section v-if="quality" class="card">
    <div class="card-head">
      <h2>数据质量闸门</h2>
      <div class="toolbar">
        <label class="field">
          <span>级别</span>
          <select v-model="errorSeverity">
            <option value="error">error</option>
            <option value="warning">warning</option>
          </select>
        </label>
        <label class="field">
          <span>检查项</span>
          <select v-model="errorCheck">
            <option value="all">全部</option>
            <option v-for="item in quality.checks" :key="item.check" :value="item.check">{{ item.label }}</option>
          </select>
        </label>
        <button class="btn" type="button" :disabled="checking" @click="runCheck">
          {{ checking ? '检查中…' : '跑一次质量检查' }}
        </button>
      </div>
    </div>
    <p class="muted" style="margin-top: 0">
      error {{ quality.errors }} · warning {{ quality.warnings }} · 有 error 的股票·日 {{ quality.purge.error_pairs }} 对，
      涉及 {{ quality.purge.codes }} 只、{{ quality.purge.days.length }} 天
    </p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>交易日</th>
            <th>代码</th>
            <th>检查项</th>
            <th>级别</th>
            <th class="wrap">说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in errors?.items ?? []" :key="row.id">
            <td class="mono">{{ row.trade_date }}</td>
            <td class="code">{{ row.stock_code }}</td>
            <td>{{ row.check_label }}</td>
            <td :class="row.severity === 'error' ? 'down' : 'flat'">{{ row.severity }}</td>
            <td class="wrap">{{ row.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!errors?.items.length" class="empty">这一级没有账</p>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>跳过账</h2>
      <div class="toolbar">
        <label class="field">
          <span>阶段</span>
          <select v-model="skipStage">
            <option value="all">全部</option>
            <option v-for="(count, stage) in status?.skips.by_stage ?? {}" :key="stage" :value="stage">
              {{ stageLabels[stage] ?? stage }}（{{ count }}）
            </option>
          </select>
        </label>
        <label class="field">
          <span>原因</span>
          <select v-model="skipReason">
            <option value="all">全部</option>
            <option v-for="reason in skipReasons" :key="reason" :value="reason">{{ reason }}</option>
          </select>
        </label>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>交易日</th>
            <th>代码</th>
            <th>阶段</th>
            <th>原因</th>
            <th class="wrap">说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in skips?.items ?? []" :key="`${row.trade_date}${row.stock_code}${row.stage}${row.reason}`">
            <td class="mono">{{ row.trade_date }}</td>
            <td class="code">{{ row.stock_code }}</td>
            <td>{{ stageLabels[row.stage] ?? row.stage }}</td>
            <td>{{ row.reason }}</td>
            <td class="wrap">{{ row.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!skips?.items.length" class="empty">没有跳过记录</p>
    </div>
  </section>
</template>
