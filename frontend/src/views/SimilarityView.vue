<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { KBar, PoolItem, SimilarityMatch, SimilarityResponse } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import type { ExportParams } from '@/api/types'
import KlineChart from '@/components/KlineChart.vue'
import OverlayChart from '@/components/OverlayChart.vue'
import ScorePanel from '@/components/ScorePanel.vue'
import { loadHealth, session } from '@/stores/session'
import { formatScore, scoreColor, shortDate } from '@/utils/format'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const codeInput = ref('')
const dateInput = ref('')
const topN = ref(20)
const loading = ref(false)
const error = ref('')
const result = ref<SimilarityResponse | null>(null)
const targetBars = ref<KBar[]>([])
const selected = ref<SimilarityMatch | null>(null)
const matchBars = ref<KBar[]>([])
const matchLoading = ref(false)
const suggestions = ref<PoolItem[]>([])
const cache = new Map<string, KBar[]>()
let requestSerial = 0
let matchSerial = 0

function barsIn(bars: KBar[], dates: string[]): KBar[] {
  const map = new Map(bars.map((bar) => [bar.date, bar]))
  return dates.map((date) => map.get(date)).filter((bar): bar is KBar => Boolean(bar))
}

const covered = computed(() => (result.value ? barsIn(targetBars.value, result.value.target.window).length : 0))
const exportParams = computed<ExportParams | null>(() => {
  if (!result.value || !/^\d{4}-\d{2}-\d{2}$/.test(dateInput.value)) return null
  return { type: 'similarity', date: dateInput.value, code: result.value.target.code, top: topN.value }
})

async function ensureDate() {
  if (!session.health) await loadHealth()
  if (!dateInput.value) dateInput.value = session.health?.data_date ?? ''
}

async function loadSuggestions() {
  try {
    const pool = await api.pool()
    const seen = new Set<string>()
    suggestions.value = pool.items.filter((item) => {
      if (seen.has(item.code)) return false
      seen.add(item.code)
      return true
    }).slice(0, 8)
  } catch {
    suggestions.value = []
  }
}

async function loadMatch(match: SimilarityMatch) {
  const serial = ++matchSerial
  const cached = cache.get(match.code)
  if (cached) {
    matchBars.value = cached
    return
  }
  matchLoading.value = true
  try {
    const response = await api.kline(match.code, 90)
    cache.set(match.code, response.bars)
    if (serial === matchSerial) matchBars.value = response.bars
  } catch (reason) {
    if (!isAbort(reason) && serial === matchSerial) {
      error.value = reason instanceof ApiError ? reason.detail : '匹配 K 线加载失败'
    }
  } finally {
    if (serial === matchSerial) matchLoading.value = false
  }
}

function selectMatch(match: SimilarityMatch) {
  selected.value = match
  void loadMatch(match)
}

async function run() {
  const code = codeInput.value.trim()
  if (!/^\d{6}$/.test(code)) {
    error.value = '请输入 6 位股票代码'
    return
  }
  await ensureDate()
  if (!dateInput.value) {
    error.value = '请选择对比日期'
    return
  }
  const serial = ++requestSerial
  loading.value = true
  error.value = ''
  try {
    const [similarity, kline] = await Promise.all([
      api.similarity({ code, date: dateInput.value, top: topN.value }),
      api.kline(code, 90),
    ])
    if (serial !== requestSerial) return
    result.value = similarity
    targetBars.value = kline.bars
    const first = similarity.matches[0] ?? null
    selected.value = first
    matchBars.value = []
    if (first) await loadMatch(first)
  } catch (reason) {
    if (serial !== requestSerial || isAbort(reason)) return
    result.value = null
    error.value = reason instanceof ApiError ? reason.detail : '查询失败'
  } finally {
    if (serial === requestSerial) loading.value = false
  }
}

async function search() {
  await run()
  const code = codeInput.value.trim()
  if (!/^\d{6}$/.test(code) || !dateInput.value || error.value) return
  const next = { code, date: dateInput.value, top: String(topN.value) }
  const same = route.query.code === next.code && route.query.date === next.date && String(route.query.top ?? '') === next.top
  if (!same) await router.replace({ name: 'similarity', query: next })
}

function useSuggestion(item: PoolItem) {
  codeInput.value = item.code
  dateInput.value = item.limit_date
  void search()
}

onMounted(async () => {
  await ensureDate()
  void loadSuggestions()
  const code = typeof route.query.code === 'string' ? route.query.code : ''
  const date = typeof route.query.date === 'string' ? route.query.date : ''
  const top = Number(route.query.top ?? 0)
  if (date) dateInput.value = date
  if (top >= 1 && top <= 100) topN.value = top
  if (/^\d{6}$/.test(code)) {
    codeInput.value = code
    await run()
  }
})

watch(
  () => route.fullPath,
  async () => {
    const code = typeof route.query.code === 'string' ? route.query.code : ''
    const date = typeof route.query.date === 'string' ? route.query.date : ''
    const top = String(route.query.top ?? topN.value)
    if (!/^\d{6}$/.test(code)) return
    if (code === codeInput.value.trim() && date === dateInput.value && top === String(topN.value) && result.value?.target.code === code) {
      return
    }
    codeInput.value = code
    if (date) dateInput.value = date
    if (route.query.top) topN.value = Number(route.query.top) || topN.value
    await run()
  },
)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>个股相似度</h1>
      <p class="lede">把这只股票的近期窗口，和近 30 个交易日涨停过的股票（比涨停前 3 根 K 线）逐一对比。已排除自身。</p>
    </div>
  </header>

  <form class="card" @submit.prevent="search">
    <div class="toolbar">
      <label class="field">
        <span>股票代码</span>
        <input v-model="codeInput" class="mono" maxlength="6" inputmode="numeric" placeholder="例如 600809" />
      </label>
      <label class="field">
        <span>截止日期</span>
        <input v-model="dateInput" class="mono" type="text" maxlength="10" placeholder="YYYY-MM-DD" />
      </label>
      <label class="field">
        <span>返回条数</span>
        <select v-model.number="topN">
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
        </select>
      </label>
      <button class="btn" type="submit" :disabled="loading">{{ loading ? '计算中…' : '开始对比' }}</button>
    </div>
  </form>

  <p v-if="error" class="alert">{{ error }}</p>

  <section v-if="!result && !loading" class="card">
    <div class="card-head"><h2>从涨停池选一只</h2></div>
    <div class="chips">
      <button v-for="item in suggestions" :key="item.code + item.limit_date" class="chip" type="button" @click="useSuggestion(item)">
        {{ item.name }} {{ item.code }}
      </button>
    </div>
    <p v-if="!suggestions.length" class="empty">输入 6 位代码后开始对比</p>
  </section>

  <div v-if="loading" class="skeleton" />

  <div v-else-if="result" class="split">
    <section class="card">
      <div class="card-head">
        <h2>相似涨停</h2>
        <div class="toolbar">
          <span class="muted">{{ result.matches.length }} / {{ result.pool_size }}</span>
          <ExportButton :params="exportParams" />
        </div>
      </div>
      <Disclaimer :text="result.disclaimer" />
      <p class="muted" style="margin-top: 0">
        股票池 {{ result.pool_dates.map(shortDate).join('、') }} · 已排除该股自己的全部样本
        <template v-if="result.stats"> · 最高 {{ formatScore(result.stats.max_similarity) }} · 平均 {{ formatScore(result.stats.avg_similarity) }} · 高度相似 {{ result.stats.high_similarity_count }}/{{ result.stats.sample_count }} · {{ result.stats.band }}</template>
      </p>
      <div v-if="!result.matches.length" class="empty">股票池里没有可对比的其他样本</div>
      <div v-else class="match-list">
        <button
          v-for="match in result.matches"
          :key="match.code + match.limit_date"
          class="match"
          :class="{ active: selected?.code === match.code && selected?.limit_date === match.limit_date }"
          type="button"
          @click="selectMatch(match)"
        >
          <div class="match-top">
            <b>{{ match.name }}</b>
            <span class="match-score" :style="{ color: scoreColor(match.score) }">{{ formatScore(match.score) }}</span>
          </div>
          <div class="match-meta">
            <span class="code">{{ match.code }}</span>
            · 涨停 {{ match.limit_date }}
          </div>
          <div v-if="match.group_scores" class="match-meta">价格 {{ formatScore(match.group_scores.price_trend) }} · K线 {{ formatScore(match.group_scores.kline_structure) }} · 量 {{ formatScore(match.group_scores.volume) }}</div>
        </button>
      </div>
    </section>

    <div class="stack">
      <section class="card">
        <div class="card-head">
          <h2>{{ result.target.name }} · 日 K</h2>
          <span class="muted code">{{ result.target.code }} · 窗口 {{ result.target.window.map(shortDate).join(' / ') }}</span>
        </div>
        <p class="hint">金色底纹是本次对比窗口。红涨绿跌。</p>
        <KlineChart v-if="targetBars.length" :bars="targetBars" :window-dates="result.target.window" />
        <p v-else class="empty">没有返回 K 线</p>
        <p v-if="targetBars.length && !covered" class="hint">返回的 K 线没有覆盖对比窗口。</p>
      </section>

      <section v-if="selected" class="card">
        <div class="card-head">
          <h2>叠加对比</h2>
          <span class="muted">价格各自相对窗口首日开盘，方便不同价位叠在一起</span>
        </div>
        <Disclaimer :text="result.disclaimer" />
        <div v-if="matchLoading" class="skeleton" />
        <OverlayChart
          v-else
          :target-name="result.target.name"
          :match-name="selected.name"
          :target-window="result.target.window"
          :match-window="selected.window"
          :target-bars="targetBars"
          :match-bars="matchBars"
        />
      </section>

      <section v-if="selected" class="card">
        <div class="card-head">
          <h2>相似度构成</h2>
          <span class="muted">{{ selected.name }} · {{ selected.limit_date }}</span>
        </div>
        <Disclaimer :text="result.disclaimer" />
        <ScorePanel
          :score="selected.score"
          :euclidean="selected.euclidean_score"
          :cosine="selected.cosine_score"
          :groups="selected.group_scores"
        />
      </section>
    </div>
  </div>
</template>
