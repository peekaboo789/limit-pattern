<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type {
  BacktestLatest,
  BacktestObservationPage,
  BacktestRunSummary,
  BacktestStatus,
} from '@/api/types'
import { formatDateTime, formatScore } from '@/utils/format'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const latest = ref<BacktestLatest | null>(null)
const status = ref<BacktestStatus | null>(null)
const observations = ref<BacktestObservationPage | null>(null)
const loading = ref(false)
const obsLoading = ref(false)
const error = ref('')
const actionError = ref('')

const startDay = ref('')
const endDay = ref('')
const maxDays = ref('all')
const obsBand = ref('all')
const obsOffset = ref(0)
const obsLimit = 100

const run = computed(() => latest.value?.run ?? null)
const busy = computed(() => status.value?.running === true)
const buckets = computed(() => run.value?.buckets ?? [])
const calibration = computed(() => run.value?.calibration ?? null)
const runs = computed(() => latest.value?.runs ?? [])
const statusWord: Record<string, string> = {
  running: '正在跑',
  done: '已完成',
  failed: '跑砸了',
}
let timer: number | null = null
let lastRunId: number | null = null

/** 发生率是 0~1 的原始比例；空是「算不出来」，不是 0%。 */
function rateText(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(2)}%`
}

function liftText(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toFixed(2)}×`
}

function metricText(value: number | null | undefined): string {
  return value == null ? '—' : value.toFixed(4)
}

function binRange(lower: number, upper: number): string {
  return `${(lower * 100).toFixed(0)}~${(upper * 100).toFixed(0)}%`
}

async function loadObservations(reset = false) {
  const runId = run.value?.run_id
  if (!runId) {
    observations.value = null
    return
  }
  if (reset) obsOffset.value = 0
  obsLoading.value = true
  try {
    observations.value = await api.backtestObservations({
      run_id: runId,
      band: obsBand.value === 'all' ? undefined : obsBand.value,
      limit: obsLimit,
      offset: obsOffset.value,
    })
  } catch (reason) {
    if (isAbort(reason)) return
    error.value = reason instanceof ApiError ? reason.detail : '明细加载失败'
  } finally {
    obsLoading.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    latest.value = await api.backtestLatest()
    status.value = await api.backtestStatus()
    if (run.value && run.value.run_id !== lastRunId) {
      lastRunId = run.value.run_id
      obsBand.value = 'all'
      obsOffset.value = 0
    }
    await loadObservations()
  } catch (reason) {
    if (isAbort(reason)) return
    error.value = reason instanceof ApiError ? reason.detail : '回测账加载失败'
  } finally {
    loading.value = false
  }
}

async function startRun() {
  actionError.value = ''
  try {
    await api.startBacktest({
      start: startDay.value || undefined,
      end: endDay.value || undefined,
      max_days: maxDays.value === 'all' ? undefined : Number(maxDays.value),
    })
    status.value = await api.backtestStatus()
    startPolling()
  } catch (reason) {
    actionError.value = reason instanceof ApiError ? reason.detail : '起不来这一轮回测'
  }
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
    const state = await api.backtestStatus()
    status.value = state
    if (!state.running) {
      stopPolling()
      await load()
    }
  } catch {
    //: 读不到状态就维持上一份，别把页面顶成一条红条。
  }
}

async function remove(item: BacktestRunSummary) {
  if (!window.confirm(`删掉第 ${item.run_id} 次回测和它的明细？原始行情不动。`)) return
  actionError.value = ''
  try {
    await api.deleteBacktest(item.run_id)
    await load()
  } catch (reason) {
    actionError.value = reason instanceof ApiError ? reason.detail : '删除失败'
  }
}

watch(obsBand, () => void loadObservations(true))

function page(delta: number) {
  obsOffset.value = Math.max(0, obsOffset.value + delta)
  void loadObservations()
}

onMounted(() => {
  void load().then(() => {
    if (busy.value) startPolling()
  })
})

onUnmounted(stopPolling)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>历史回测</h1>
      <p class="lede">逐日重算全市场形态相似度，再看下一交易日涨停了没有；落下的分档发生率就是榜上概率列的出处。</p>
    </div>
    <div class="toolbar">
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">
        {{ loading ? '读账中…' : '刷新' }}
      </button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>

  <section class="card">
    <div class="card-head">
      <h2>跑一轮</h2>
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
          <span>最多跑</span>
          <select v-model="maxDays">
            <option value="all">库里全部</option>
            <option value="3">3 天</option>
            <option value="5">5 天</option>
            <option value="10">10 天</option>
          </select>
        </label>
        <button class="btn" type="button" :disabled="busy || loading" @click="startRun">
          {{ busy ? '跑着…' : '开始回测' }}
        </button>
      </div>
    </div>
    <p v-if="actionError" class="alert">{{ actionError }}</p>

    <template v-if="busy && status">
      <div class="progress running"><i :style="{ '--w': `${status.progress}%` }" /></div>
      <div class="readonly" style="margin-top: 8px">
        <div class="two">
          <div>
            <div><span class="muted">进度</span> <b>{{ status.progress }}%</b><span class="muted"> · {{ status.days_done }}/{{ status.days_total }} 天</span></div>
            <div><span class="muted">当前</span> <b>{{ status.current_date || '—' }}</b></div>
          </div>
          <div>
            <div><span class="muted">这一轮</span> <b>{{ status.run_id ? `#${status.run_id}` : '—' }}</b></div>
            <div><span class="muted">说明</span> <b>{{ status.message }}</b></div>
          </div>
        </div>
      </div>
    </template>
    <p v-else-if="status?.error" class="alert">{{ status.error }}</p>
    <p v-else class="hint">{{ status?.message || '还没有跑过回测' }}<template v-if="status?.last_finished_at"> · 上一轮结束于 {{ formatDateTime(status.last_finished_at) }}</template></p>
  </section>

  <template v-if="run">
    <section class="stats">
      <article class="card stat">
        <div class="stat-label">交易日</div>
        <div class="stat-value">{{ run.day_count }}</div>
        <div class="stat-foot">{{ run.start_date }} ~ {{ run.end_date }}</div>
      </article>
      <article class="card stat">
        <div class="stat-label">观测</div>
        <div class="stat-value">{{ run.observation_count }}</div>
        <div class="stat-foot">第 {{ run.run_id }} 次回测 · {{ formatDateTime(run.finished_at) }}</div>
      </article>
      <article class="card stat">
        <div class="stat-label">基准发生率</div>
        <div class="stat-value" style="font-size: 20px">{{ rateText(run.baseline_rate) }}</div>
        <div class="stat-foot">全样本次日涨停比例</div>
      </article>
      <article class="card stat">
        <div class="stat-label">校准 ECE</div>
        <div class="stat-value" style="font-size: 20px">{{ calibration?.available ? metricText(calibration.ece) : '未校准' }}</div>
        <div class="stat-foot">{{ calibration?.available ? `Brier ${metricText(calibration.brier)} · 对数损失 ${metricText(calibration.log_loss)}` : calibration?.reason || '原因未记' }}</div>
      </article>
    </section>

    <p v-if="!run.leakage.passed" class="alert">
      防泄漏自检未通过：{{ run.leakage.detail }}
    </p>
    <section v-else class="card">
      <div class="card-head">
        <h2>防泄漏自检</h2>
        <span class="tag tag-hot">通过</span>
      </div>
      <p class="hint">{{ run.leakage.detail }}</p>
      <div v-if="run.leakage.spot_checks.length" class="table-wrap" style="max-height: 240px; margin-top: 8px">
        <table>
          <thead>
            <tr>
              <th>抽验股票</th>
              <th>交易日</th>
              <th class="right">落库</th>
              <th class="right">重算</th>
              <th class="right">回测池</th>
              <th class="right">生产池</th>
              <th>判据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in run.leakage.spot_checks" :key="`${item.stock_code}-${item.trade_date}`">
              <td class="code">{{ item.stock_code }}</td>
              <td>{{ item.trade_date }}</td>
              <td class="right">{{ formatScore(item.recorded) }}</td>
              <td class="right">{{ formatScore(item.recomputed) }}</td>
              <td class="right">{{ item.backtest_pool ?? '—' }}</td>
              <td class="right">{{ item.production_pool ?? '—' }}</td>
              <td>
                <span v-if="item.compared" class="tag" :class="item.ok ? 'tag-hot' : 'type-weak'">{{ item.ok ? '对得上' : '对不上' }}</span>
                <span v-else class="hint">{{ item.reason || '没比' }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>分档发生率</h2>
        <span class="muted">全样本基准 {{ rateText(run.baseline_rate) }} · 每档至少 {{ latest?.probability.min_band_observations ?? 50 }} 条观测</span>
      </div>
      <div v-if="!buckets.length" class="empty">这一轮没有落下一档统计</div>
      <div v-else class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>档位</th>
              <th class="right">有结果的观测</th>
              <th class="right">次日涨停</th>
              <th class="right">发生率</th>
              <th class="right">相对基准</th>
              <th class="right">待揭晓</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="bucket in buckets" :key="bucket.band">
              <td><span class="tag">{{ bucket.band }}</span></td>
              <td class="right">{{ bucket.sample_count }}</td>
              <td class="right">{{ bucket.limit_up_count }}</td>
              <td class="right"><b>{{ rateText(bucket.limit_up_rate) }}</b></td>
              <td class="right">{{ liftText(bucket.lift) }}</td>
              <td class="right">{{ bucket.pending_count }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="calibration" class="card">
      <div class="card-head">
        <h2>概率校准</h2>
        <span class="muted">{{ calibration.available ? `前 ${Math.round(calibration.split_ratio * 100)}% 训练 / 后段检验` : calibration.reason }}</span>
      </div>
      <template v-if="calibration.available">
        <div class="readonly">
          <div class="two">
            <div>
              <div><span class="muted">训练</span> <b>{{ calibration.train_days }} 天 · {{ calibration.train_count }} 条</b></div>
              <div><span class="muted">检验</span> <b>{{ calibration.test_days }} 天 · {{ calibration.test_count }} 条</b></div>
            </div>
            <div>
              <div><span class="muted">Brier</span> <b>{{ metricText(calibration.brier) }}</b><span class="muted"> · 越小越好</span></div>
              <div><span class="muted">对数损失</span> <b>{{ metricText(calibration.log_loss) }}</b><span class="muted"> · 越小越好</span></div>
            </div>
          </div>
        </div>
        <div v-if="calibration.bins.length" class="table-wrap" style="max-height: 300px; margin-top: 10px">
          <table>
            <thead>
              <tr>
                <th>概率段</th>
                <th class="right">预测均值</th>
                <th class="right">实际发生率</th>
                <th class="right">条数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="bin in calibration.bins" :key="`${bin.lower}-${bin.upper}`">
                <td>{{ binRange(bin.lower, bin.upper) }}</td>
                <td class="right">{{ rateText(bin.mean_predicted) }}</td>
                <td class="right">{{ rateText(bin.observed_rate) }}</td>
                <td class="right">{{ bin.count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="calibration.band_checks.length" class="table-wrap" style="max-height: 300px; margin-top: 10px">
          <table>
            <thead>
              <tr>
                <th>档位</th>
                <th class="right">训练给出的概率</th>
                <th class="right">检验段实际</th>
                <th class="right">条数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="check in calibration.band_checks" :key="check.band">
                <td><span class="tag">{{ check.band }}</span></td>
                <td class="right">{{ rateText(check.predicted) }}</td>
                <td class="right">{{ rateText(check.observed) }}</td>
                <td class="right">{{ check.count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>逐日</h2>
        <span class="muted">{{ run.days.length }} 天 · 标签日取各日之后最近一个有行情的日子</span>
      </div>
      <div v-if="!run.days.length" class="empty">这一轮没有逐日记录</div>
      <div v-else class="table-wrap" style="max-height: 360px">
        <table>
          <thead>
            <tr>
              <th>交易日</th>
              <th class="right">涨停</th>
              <th class="right">样本池</th>
              <th class="right">参与打分</th>
              <th class="right">跳过样本</th>
              <th class="right">缺 K 线</th>
              <th class="right">脏行剔</th>
              <th>标签日</th>
              <th>样本窗口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="day in run.days" :key="day.trade_date">
              <td>{{ day.trade_date }}</td>
              <td class="right">{{ day.limit_up_count }}</td>
              <td class="right">{{ day.pool_size }}</td>
              <td class="right">{{ day.scanned_count }}</td>
              <td class="right">{{ day.skipped_samples }}</td>
              <td class="right">{{ day.missing_bars }}</td>
              <td class="right">{{ day.dirty_skipped }}</td>
              <td>{{ day.next_trade_date || '—' }}</td>
              <td><span class="tag" :class="day.sample_window_ok ? '' : 'type-weak'">{{ day.sample_window_ok ? '严格在前' : '有问题' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>明细分页</h2>
        <div class="toolbar">
          <label class="field">
            <span>档位</span>
            <select v-model="obsBand">
              <option value="all">全部</option>
              <option v-for="bucket in buckets" :key="bucket.band" :value="bucket.band">{{ bucket.band }}</option>
            </select>
          </label>
          <span class="muted">共 {{ observations?.total ?? 0 }} 条</span>
          <button class="btn btn-ghost" type="button" :disabled="obsLoading || obsOffset === 0" @click="page(-obsLimit)">上一页</button>
          <button class="btn btn-ghost" type="button" :disabled="obsLoading || (observations?.total ?? 0) <= obsOffset + obsLimit" @click="page(obsLimit)">下一页</button>
        </div>
      </div>
      <div v-if="!observations?.items.length" class="empty">{{ obsLoading ? '读明细中…' : '这个条件下没有明细' }}</div>
      <div v-else class="table-wrap" style="max-height: 420px">
        <table>
          <thead>
            <tr>
              <th>交易日</th>
              <th>股票</th>
              <th class="right">最高相似</th>
              <th class="right">平均</th>
              <th class="right">样本</th>
              <th class="right">高相似</th>
              <th>档位</th>
              <th>次日</th>
              <th class="right">次日涨幅</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in observations.items" :key="`${item.trade_date}-${item.stock_code}`">
              <td>{{ item.trade_date }}</td>
              <td>
                <div>{{ item.stock_name }}</div>
                <div class="code">{{ item.stock_code }}</div>
              </td>
              <td class="right">{{ formatScore(item.max_similarity) }}</td>
              <td class="right">{{ formatScore(item.avg_similarity) }}</td>
              <td class="right">{{ item.sample_count }}</td>
              <td class="right">{{ item.high_similarity_count }}</td>
              <td><span class="tag">{{ item.band }}</span></td>
              <td>{{ item.next_date || '—' }}</td>
              <td class="right">{{ item.next_change_percent == null ? '—' : `${item.next_change_percent.toFixed(2)}%` }}</td>
              <td>
                <span v-if="item.next_limit_up === true" class="tag tag-hot">涨停</span>
                <span v-else-if="item.next_limit_up === false" class="tag type-weak">没涨停</span>
                <span v-else class="hint">待揭晓</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </template>

  <section class="card">
    <div class="card-head">
      <h2>回测台账</h2>
      <span class="muted">含正在跑与跑砸的那几行</span>
    </div>
    <p v-if="actionError" class="alert">{{ actionError }}</p>
    <div v-if="!runs.length" class="empty">库里还没有回测记录</div>
    <div v-else class="table-wrap" style="max-height: 360px">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>区间</th>
            <th>状态</th>
            <th class="right">交易日</th>
            <th class="right">观测</th>
            <th class="right">基准</th>
            <th>开始</th>
            <th>结束</th>
            <th>账</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in runs" :key="item.run_id">
            <td class="rank">{{ item.run_id }}</td>
            <td>{{ item.start_date }} ~ {{ item.end_date }}</td>
            <td>
              <span class="tag" :class="item.status === 'done' ? 'tag-hot' : item.status === 'failed' ? 'type-weak' : 'type-normal'">
                {{ statusWord[item.status] ?? '状态未知' }}
              </span>
            </td>
            <td class="right">{{ item.day_count }}</td>
            <td class="right">{{ item.observation_count }}</td>
            <td class="right">{{ rateText(item.baseline_rate) }}</td>
            <td>{{ formatDateTime(item.created_at) }}</td>
            <td>{{ formatDateTime(item.finished_at) }}</td>
            <td class="hint wrap">{{ item.error || item.note || '—' }}</td>
            <td>
              <button class="btn btn-ghost" type="button" @click="remove(item)">删</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="hint" style="margin-top: 12px">{{ latest?.disclaimer }}</p>
  </section>
</template>
