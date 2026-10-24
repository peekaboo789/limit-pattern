<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type {
  CompareResponse,
  KBar,
  MatchSummaryRow,
  PoolItem,
  SimilarityMatch,
  SimilarityResponse,
  StoredMatchesResponse,
} from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import type { ExportParams } from '@/api/types'
import KlineChart from '@/components/KlineChart.vue'
import OverlayChart from '@/components/OverlayChart.vue'
import ScorePanel from '@/components/ScorePanel.vue'
import { loadHealth, session } from '@/stores/session'
import { formatScore, scoreColor, shortDate } from '@/utils/format'
import { saveExportedText } from '@/utils/saveFile'
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
const stored = ref<StoredMatchesResponse | null>(null)
const storedLoading = ref(false)
const storedError = ref('')
const recomputing = ref(false)
const compare = ref<CompareResponse | null>(null)
const compareLoading = ref(false)
const compareError = ref('')
const compareKey = ref<{ code: string; limit_date: string; name: string } | null>(null)
const exporting = ref(false)
const exportNote = ref('')
let requestSerial = 0
let matchSerial = 0
let storedSerial = 0
let compareSerial = 0

const summary = computed<MatchSummaryRow | null>(() => stored.value?.summary ?? null)
const aggregateNames = computed(() => {
  const keys = new Set<string>()
  for (const name of Object.keys(compare.value?.target.aggregates ?? {})) keys.add(name)
  for (const name of Object.keys(compare.value?.sample.aggregates ?? {})) keys.add(name)
  return [...keys]
})

function barsIn(bars: KBar[], dates: string[]): KBar[] {
  const map = new Map(bars.map((bar) => [bar.date, bar]))
  return dates.map((date) => map.get(date)).filter((bar): bar is KBar => Boolean(bar))
}

/** 特征值是比例与百分数：一位小数会把 1.234 和 1.231 抹成同一个数，差值那列就没得看了。 */
function num(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
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
  void openCompare(match)
}

/** 库里那一轮的明细：GET 只读已落库的那份，页面上这两个数才和回测、AI 用的是同一批行。 */
async function loadStored(code: string, day: string) {
  const serial = ++storedSerial
  storedLoading.value = true
  try {
    const response = await api.storedMatches(code, day)
    if (serial === storedSerial) {
      stored.value = response
      storedError.value = ''
    }
  } catch (reason) {
    if (serial !== storedSerial) return
    stored.value = null
    storedError.value = reason instanceof ApiError ? reason.detail : '落库明细读取失败'
  } finally {
    if (serial === storedSerial) storedLoading.value = false
  }
}

async function recomputeStored() {
  const code = result.value?.target.code
  if (!code) return
  recomputing.value = true
  storedError.value = ''
  try {
    const response = await api.recomputeMatches(code, dateInput.value)
    stored.value = response
    if (!response.ok) storedError.value = response.detail
  } catch (reason) {
    stored.value = null
    storedError.value = reason instanceof ApiError ? reason.detail : '重算失败'
  } finally {
    recomputing.value = false
  }
}

async function openCompare(sample: { code: string; limit_date: string; name: string }) {
  const code = result.value?.target.code
  if (!code) return
  const serial = ++compareSerial
  compareKey.value = sample
  compareLoading.value = true
  compareError.value = ''
  try {
    const response = await api.compareMatch({
      code,
      sample_code: sample.code,
      date: dateInput.value,
      sample_date: sample.limit_date,
    })
    if (serial === compareSerial) compare.value = response
  } catch (reason) {
    if (serial !== compareSerial) return
    compare.value = null
    compareError.value = reason instanceof ApiError ? reason.detail : '逐日对比读取失败'
  } finally {
    if (serial === compareSerial) compareLoading.value = false
  }
}

async function exportCsv() {
  const code = result.value?.target.code
  if (!code) return
  exporting.value = true
  exportNote.value = ''
  try {
    const file = await api.exportMatchesCsv({ date: dateInput.value, code })
    const mode = await saveExportedText(file.filename, file.text, file.blob)
    if (mode === 'cancel') exportNote.value = '已取消保存'
    else if (!file.rows) exportNote.value = '库里这一天这一只没有明细，导出的是只有表头的空表'
    else if (file.truncated) exportNote.value = `${file.rows} 行（只含当天最高相似度靠前的那几只，其余没导）`
    else exportNote.value = `${file.rows} 行`
  } catch (reason) {
    exportNote.value = reason instanceof ApiError ? reason.detail : 'CSV 导出失败'
  } finally {
    exporting.value = false
  }
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
    compare.value = null
    compareKey.value = null
    compareError.value = ''
    exportNote.value = ''
    void loadStored(code, dateInput.value)
    if (first) {
      await loadMatch(first)
      void openCompare(first)
    }
  } catch (reason) {
    if (serial !== requestSerial || isAbort(reason)) return
    result.value = null
    stored.value = null
    compare.value = null
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
      <p class="lede">把这只股票的近期窗口，和最近三个交易日涨停过的股票逐一对比。已排除自身。</p>
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
        股票池 {{ result.pool_dates.map(shortDate).join('、') }} · 已排除自身
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
          <div class="match-meta">形态 {{ formatScore(match.shape_score) }} · 量 {{ formatScore(match.volume_score) }} · 量比 {{ formatScore(match.volume_pct_score) }}</div>
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
          <h2>分项得分</h2>
          <span class="muted">{{ selected.name }} · {{ selected.limit_date }}</span>
        </div>
        <Disclaimer :text="result.disclaimer" />
        <ScorePanel
          :score="selected.score"
          :shape="selected.shape_score"
          :volume="selected.volume_score"
          :volume-pct="selected.volume_pct_score"
          :other="selected.other_scores"
        />
      </section>

      <section v-if="compareKey" class="card">
        <div class="card-head">
          <h2>逐日对比</h2>
          <span class="muted">
            {{ result.target.code }} {{ shortDate(compare?.target.as_of_date ?? dateInput) }} ·
            {{ compareKey.code }} {{ shortDate(compareKey.limit_date) }}
          </span>
        </div>
        <div v-if="compareLoading" class="skeleton" />
        <p v-else-if="compareError" class="alert">{{ compareError }}</p>
        <template v-else-if="compare">
          <Disclaimer :text="compare.disclaimer" />
          <p class="muted" style="margin-top: 0">
            落库分 {{ compare.score === null ? '库里没有这一对' : formatScore(compare.score) }}
          </p>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>日</th>
                  <th>字段</th>
                  <th class="right">{{ compare.target.code }}</th>
                  <th class="right">{{ compare.sample.code }}</th>
                  <th class="right">差值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in compare.differences" :key="`${row.day}-${row.field}-${index}`">
                  <td class="mono muted">{{ row.day }}</td>
                  <td>{{ row.field }}</td>
                  <td class="right">{{ num(row.target) }}</td>
                  <td class="right">{{ num(row.sample) }}</td>
                  <td class="right mono">{{ num(row.difference) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="table-wrap" style="max-height: none">
            <table>
              <thead>
                <tr>
                  <th>三日聚合</th>
                  <th class="right">{{ compare.target.code }}</th>
                  <th class="right">{{ compare.sample.code }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="name in aggregateNames" :key="name">
                  <td class="muted">{{ name }}</td>
                  <td class="right">{{ num(compare.target.aggregates[name]) }}</td>
                  <td class="right">{{ num(compare.sample.aggregates?.[name]) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </section>
    </div>
  </div>

  <section v-if="result" class="card" style="margin-top: 14px">
    <div class="card-head">
      <h2>库里落库明细</h2>
      <div class="toolbar">
        <button class="btn btn-ghost" type="button" :disabled="recomputing" @click="recomputeStored">
          {{ recomputing ? '重算中…' : '重算这一只' }}
        </button>
        <button class="btn btn-ghost" type="button" :disabled="exporting" @click="exportCsv">
          {{ exporting ? '导出中…' : '导出 CSV' }}
        </button>
      </div>
    </div>
    <p v-if="exportNote" class="muted" style="margin-top: 0">{{ exportNote }}</p>
    <p v-if="storedError" class="alert">{{ storedError }}</p>
    <div v-if="storedLoading && !stored" class="skeleton" />
    <template v-else-if="stored">
      <p v-if="!stored.ok" class="alert">{{ stored.detail }}</p>
      <template v-else>
        <div class="stats">
          <div class="card stat">
            <div class="stat-label">落库样本</div>
            <div class="stat-value">{{ summary?.sample_count ?? stored.items.length }}</div>
            <div class="stat-foot">{{ stored.pool_dates.map(shortDate).join('、') }}</div>
          </div>
          <div class="card stat">
            <div class="stat-label">高相似</div>
            <div class="stat-value">{{ summary?.high_similarity_count ?? '—' }}</div>
            <div class="stat-foot">{{ summary?.high_similarity_rate === null ? '未设阈值' : `${summary?.high_similarity_rate ?? '—'}%` }}</div>
          </div>
          <div class="card stat">
            <div class="stat-label">最高相似</div>
            <div class="stat-value">{{ formatScore(summary?.max_similarity ?? null) }}</div>
            <div class="stat-foot">{{ summary?.band ?? '—' }}</div>
          </div>
          <div class="card stat">
            <div class="stat-label">平均相似</div>
            <div class="stat-value">{{ formatScore(summary?.avg_similarity ?? null) }}</div>
            <div class="stat-foot">对比池 {{ summary?.pool_size ?? stored.pool_size }} 只</div>
          </div>
        </div>
        <div class="table-wrap" style="margin-top: 12px">
          <table>
            <thead>
              <tr>
                <th class="right">#</th>
                <th>代码</th>
                <th>名称</th>
                <th>涨停日</th>
                <th class="right">分</th>
                <th>档位</th>
                <th>形态三天</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in stored.items"
                :key="item.code + item.limit_date"
                :class="{ active: compareKey?.code === item.code && compareKey?.limit_date === item.limit_date }"
                @click="openCompare(item)"
              >
                <td class="right muted">{{ item.rank }}</td>
                <td class="code">{{ item.code }}</td>
                <td>{{ item.name }}</td>
                <td class="mono">{{ shortDate(item.limit_date) }}</td>
                <td class="right" :style="{ color: scoreColor(item.score) }">{{ formatScore(item.score) }}</td>
                <td class="muted">{{ item.band }}</td>
                <td class="mono muted">{{ item.window.map(shortDate).join(' / ') }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="!stored.items.length" class="empty">库里这一天的明细是空的</p>
        </div>
      </template>
    </template>
  </section>
</template>
