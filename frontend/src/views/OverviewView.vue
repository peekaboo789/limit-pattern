<script setup lang="ts">
import ManualCapture from '@/components/ManualCapture.vue'
import { api, ApiError, isAbort } from '@/api/client'
import AiOverviewCard from '@/components/AiOverviewCard.vue'
import Disclaimer from '@/components/Disclaimer.vue'
import AiInputExport from '@/components/AiInputExport.vue'
import ExportButton from '@/components/ExportButton.vue'
import type { ExportParams } from '@/api/types'
import type { PoolResponse, ScanResponse } from '@/api/types'
import { cancelSync, loadHealth, session, startFullSync, startSync } from '@/stores/session'
import { boardLabel, clampPercent, formatDateTime, formatScore, scoreColor, shortDate } from '@/utils/format'
import { sourceLabel } from '@/utils/labels'
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const date = ref('')
const topN = ref(50)
const minScore = ref(0)
const loading = ref(false)
const error = ref('')
const pool = ref<PoolResponse | null>(null)
const scan = ref<ScanResponse | null>(null)

const counts = computed(() => {
  const dates = pool.value?.dates ?? []
  return dates.map((day) => ({
    date: day,
    count: pool.value?.items.filter((item) => item.limit_date === day).length ?? 0,
  }))
})

const total = computed(() => pool.value?.items.length ?? 0)
const exportParams = computed<ExportParams | null>(() => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date.value)) return null
  return { type: 'scan', date: date.value, top: topN.value }
})
const progress = computed(() => Math.max(0, Math.min(100, session.sync?.progress ?? 0)))
const busy = computed(() => session.startingSync || Boolean(session.sync?.running))
const syncDetail = computed(() => {
  const sync = session.sync
  if (!sync?.total) return ''
  return `${sync.done ?? 0}/${sync.total} · 已写入 ${sync.saved ?? 0} · 失败 ${sync.failed ?? 0}`
})
const sourceText = computed(() => sourceLabel(session.sync?.active_source || session.health?.active_source || session.health?.data_source || ''))

async function reload() {
  if (!date.value) return
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date.value)) {
    error.value = '日期格式应为 YYYY-MM-DD'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const [poolResult, scanResult] = await Promise.all([
      api.pool(),
      api.scan({ date: date.value, top: topN.value, min_score: minScore.value }),
    ])
    pool.value = poolResult
    scan.value = scanResult
  } catch (reason) {
    if (isAbort(reason)) return
    error.value = reason instanceof ApiError ? reason.detail : '总览加载失败'
  } finally {
    loading.value = false
  }
}

function open(code: string) {
  void router.push({ name: 'similarity', query: { code, date: scan.value?.date || date.value } })
}

async function syncNow() {
  await startSync()
}

const fullDays = ref(60)
const fullNote = ref('')
async function syncFull() {
  const days = Number(fullDays.value)
  if (!Number.isInteger(days) || days < 5 || days > 250) {
    fullNote.value = '天数要是 5 到 250 的整数'
    return
  }
  fullNote.value = (await startFullSync(days)) || `开始补全最近 ${days} 个交易日`
}

onMounted(async () => {
  if (!session.health) await loadHealth()
  date.value = session.health?.data_date ?? ''
  await reload()
})

watch(
  () => session.health?.data_date,
  (value) => {
    if (value && !date.value) {
      date.value = value
      void reload()
    }
  },
)

const pausedSources = computed(() =>
  Object.entries(session.sync?.sources ?? {})
    .filter(([, info]) => info.paused)
    .map(([name]) => name),
)

watch(
  () => session.sync?.running,
  (running, previous) => {
    if (previous && !running) {
      void loadHealth().then(() => reload())
    }
  },
)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>总览</h1>
      <p class="lede">近 30 个交易日（设置可改）的涨停样本，以及全市场与该样本池的形态扫描。</p>
    </div>
  </header>

  <ManualCapture compact />

  <section class="card">
    <div class="card-head">
      <h2>行情同步</h2>
      <span class="muted">上次 {{ formatDateTime(session.sync?.last_sync_at) }}</span>
    </div>
    <div class="toolbar">
      <button class="btn" type="button" :disabled="busy" @click="syncNow">
        {{ busy ? '同步中…' : '同步行情' }}
      </button>
      <label class="muted">补全最近 <input v-model.number="fullDays" type="number" min="5" max="250" step="1" style="width: 64px" /> 个交易日</label>
      <button class="btn btn-ghost" type="button" :disabled="busy" title="即使今天已有数据也重新下载，并补写缺失日期的统计总结" @click="syncFull">补全历史</button>
      <button v-if="session.sync?.running" class="btn btn-ghost" type="button" :disabled="session.sync?.cancelling" @click="cancelSync">
        {{ session.sync?.cancelling ? '取消中…' : '取消同步' }}
      </button>
      <span class="muted">{{ session.sync?.message || '等待同步' }}</span>
      <span v-if="session.sync?.running && session.sync?.current_source" class="muted">当前源 {{ sourceLabel(session.sync.current_source) }}</span>
      <span v-if="syncDetail" class="muted">{{ syncDetail }}</span>
      <span v-if="sourceText" class="muted">数据源 {{ sourceText }}</span>
    </div>
    <p class="muted" style="margin: 8px 0 0">
      已存行情 {{ session.health?.stored_days ?? 0 }} 个交易日<template v-if="session.health?.earliest_date">（{{ session.health.earliest_date }} ~ {{ session.health.data_date }}）</template>
      · 生效：样本池 {{ session.health?.sample_pool_days ?? '-' }} 日，保留 {{ session.health?.retention_days ?? '-' }} 日
    </p>
    <p v-if="fullNote" class="muted" style="margin: 4px 0 0">{{ fullNote }}</p>
    <div class="progress" :class="{ running: session.sync?.running }" style="margin-top: 12px" role="progressbar" :aria-valuenow="progress">
      <i :style="{ '--w': `${progress}%` }" />
    </div>
    <p v-if="session.syncError" class="alert" style="margin-top: 10px">{{ session.syncError }}</p>
    <p v-if="pausedSources.length" class="muted" style="margin-top: 8px">暂停使用（连续失败，约 90 秒后再试）：{{ pausedSources.map(sourceLabel).join('、') }}</p>
    <details v-if="session.sync?.errors?.length" style="margin-top: 8px">
      <summary class="muted">最近错误 {{ session.sync.errors.length }} 条</summary>
      <div v-for="(line, index) in session.sync.errors" :key="index" class="muted mono" style="font-size: 12px">{{ line }}</div>
    </details>
  </section>

  <AiOverviewCard />

  <section class="stats">
    <article class="card stat">
      <div class="stat-label">近 30 日涨停样本</div>
      <div class="stat-value">{{ total }}</div>
      <div class="stat-foot">含同一只股票的多次涨停</div>
    </article>
    <article v-for="item in counts" :key="item.date" class="card stat">
      <div class="stat-label">{{ item.date }}</div>
      <div class="stat-value">{{ item.count }}</div>
      <div class="stat-foot">涨停 {{ item.count }} 只</div>
    </article>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>形态扫描排行</h2>
      <div class="toolbar">
        <span class="muted" v-if="scan">样本池 {{ scan.pool_size }} 条 · {{ scan.date }}</span>
        <ExportButton :params="exportParams" />
        <AiInputExport :date="exportParams?.date" />
      </div>
    </div>
    <Disclaimer :text="scan?.disclaimer" />
    <div class="toolbar" style="margin-bottom: 12px">
      <label class="field">
        <span>扫描日期</span>
        <input v-model="date" class="mono" type="text" maxlength="10" placeholder="YYYY-MM-DD" @change="reload" />
      </label>
      <label class="field">
        <span>数量</span>
        <select v-model.number="topN" @change="reload">
          <option :value="20">20</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
      <label class="field" style="min-width: 180px">
        <span>最低分 {{ minScore }}</span>
        <input v-model.number="minScore" type="range" min="0" max="90" step="5" @change="reload" />
      </label>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="reload">刷新</button>
    </div>
    <p v-if="error" class="alert">{{ error }}</p>
    <div v-else-if="loading" class="skeleton" />
    <div v-else-if="!scan?.items.length" class="empty">没有达到最低分的股票</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>代码</th>
            <th>名称</th>
            <th title="对样本池（已去掉该股自己的全部样本）逐一比，取最大值；选前 100 就用它">最高相似度</th>
            <th>最相似涨停</th>
            <th class="right">平均</th>
            <th class="right" title="相似度 ≥ 高度阈值（默认 90）的样本数 / 比对样本数">高度相似</th>
            <th class="right">档位</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(row, index) in scan.items"
            :key="row.code"
            tabindex="0"
            @click="open(row.code)"
            @keydown.enter="open(row.code)"
          >
            <td class="rank" :class="{ top: index < 3 }">{{ String(index + 1).padStart(2, '0') }}</td>
            <td class="code">{{ row.code }}</td>
            <td>{{ row.name }}</td>
            <td>
              <span class="meter"><i :style="{ '--w': `${clampPercent(row.score)}%`, background: scoreColor(row.score) }" /></span>
              <b :style="{ color: scoreColor(row.score) }">{{ formatScore(row.max_similarity ?? row.score) }}</b>
            </td>
            <td class="wrap">
              <div>{{ row.best_match.name }}</div>
              <div class="muted">
                <span class="code">{{ row.best_match.code }}</span>
                · {{ shortDate(row.best_match.limit_date) }}
                <span class="pill" :class="row.best_match.code.startsWith('6') ? 'pill-sh' : 'pill-sz'">
                  {{ boardLabel(row.best_match.code.startsWith('6') ? 'SH' : 'SZ') }}
                </span>
              </div>
            </td>
            <td class="right">{{ formatScore(row.avg_similarity) }}</td>
            <td class="right">{{ row.high_similarity_count ?? '—' }}/{{ row.sample_count ?? '—' }}</td>
            <td class="right">{{ row.band || '—' }}<span v-if="row.is_limit_up" class="chip" style="margin-left: 4px">已涨停</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
