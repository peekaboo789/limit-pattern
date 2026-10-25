<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import type { ExportParams } from '@/api/types'
import type { PoolResponse, ScanResponse } from '@/api/types'
import { compareWindowText, loadHealth, session, startSync } from '@/stores/session'
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
      <p class="lede">{{ compareWindowText() }}的涨停样本，以及全市场与该样本池的形态扫描。</p>
    </div>
  </header>

  <section class="card">
    <div class="card-head">
      <h2>行情同步</h2>
      <span class="muted">上次 {{ formatDateTime(session.sync?.last_sync_at) }}</span>
    </div>
    <div class="toolbar">
      <button class="btn" type="button" :disabled="busy" @click="syncNow">
        {{ busy ? '同步中…' : '同步行情' }}
      </button>
      <span class="muted">{{ session.sync?.message || '等待同步' }}</span>
      <span v-if="syncDetail" class="muted">{{ syncDetail }}</span>
      <span v-if="sourceText" class="muted">数据源 {{ sourceText }}</span>
    </div>
    <div class="progress" :class="{ running: session.sync?.running }" style="margin-top: 12px" role="progressbar" :aria-valuenow="progress">
      <i :style="{ '--w': `${progress}%` }" />
    </div>
    <p v-if="session.syncError" class="alert" style="margin-top: 10px">{{ session.syncError }}</p>
  </section>

  <section class="stats">
    <article class="card stat">
      <div class="stat-label">{{ compareWindowText() }}涨停样本</div>
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
            <th>综合相似度</th>
            <th>最相似涨停</th>
            <th class="right">形态</th>
            <th class="right">成交量</th>
            <th class="right">量比</th>
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
              <b :style="{ color: scoreColor(row.score) }">{{ formatScore(row.score) }}</b>
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
            <td class="right">{{ formatScore(row.shape_score) }}</td>
            <td class="right">{{ formatScore(row.volume_score) }}</td>
            <td class="right">{{ formatScore(row.volume_pct_score) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
