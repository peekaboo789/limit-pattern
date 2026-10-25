<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type {
  KBar,
  KlineResponse,
  LimitUpItem,
  PatternDetailResponse,
  PickHistoryResponse,
  StoredMatchesResponse,
} from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import KlineChart from '@/components/KlineChart.vue'
import { loadHealth, session } from '@/stores/session'
import {
  clampPercent,
  formatAmount,
  formatDateTime,
  formatPct,
  formatPrice,
  formatScore,
  formatTime,
  formatTurnover,
  formatVolume,
  scoreColor,
  shortDate,
} from '@/utils/format'
import { limitTypeClass, verdictClass, verdictLabel } from '@/utils/labels'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const DEFAULT_DAYS = 60
const DAYS = [30, 60, 90, 120]

const code = ref('')
const date = ref('')
const days = ref(DEFAULT_DAYS)

const kline = ref<KlineResponse | null>(null)
const stored = ref<StoredMatchesResponse | null>(null)
const patterns = ref<PatternDetailResponse | null>(null)
const picks = ref<PickHistoryResponse | null>(null)
const limitRow = ref<LimitUpItem | null>(null)

const loading = ref(false)
const pageError = ref('')
/** 五块读数各自记账：一块读不到不顶掉其他四块，也不替它编一个空对象。 */
const failed = ref({ kline: '', matches: '', patterns: '', picks: '', limitups: '' })
let serial = 0
let loadedKey = ''

const bars = computed<KBar[]>(() => kline.value?.bars ?? [])
const summary = computed(() => stored.value?.summary ?? null)
const markCount = computed(() => bars.value.filter((bar) => bar.is_limit_up === true).length)
const unknownMarks = computed(() => bars.value.filter((bar) => bar.is_limit_up == null).length)
const history = computed(() => picks.value?.history ?? null)
const samples = computed(() => patterns.value?.samples ?? [])

function resetCards() {
  kline.value = null
  stored.value = null
  patterns.value = null
  picks.value = null
  limitRow.value = null
  failed.value = { kline: '', matches: '', patterns: '', picks: '', limitups: '' }
}

function readRoute() {
  const param = typeof route.params.code === 'string' ? route.params.code : ''
  code.value = /^\d{6}$/.test(param) ? param : ''
  const text = typeof route.query.date === 'string' ? route.query.date : ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) date.value = text
  const next = Number(route.query.days ?? 0)
  if (DAYS.includes(next)) days.value = next
}

async function writeUrl() {
  const text: Record<string, string> = {}
  if (date.value) text.date = date.value
  if (days.value !== DEFAULT_DAYS) text.days = String(days.value)
  const current: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) if (typeof value === 'string') current[key] = value
  const same =
    Object.keys(text).length === Object.keys(current).length &&
    Object.entries(text).every(([key, value]) => current[key] === value)
  if (same) return
  await router.replace({ name: 'stock', params: { code: code.value }, query: text })
}

function failText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError ? reason.detail : fallback
}

/** 只换 K 线根数：另外四块读数和它无关，不该跟着重敲一遍。 */
async function reloadKline() {
  if (!code.value) return
  const token = ++serial
  loading.value = true
  try {
    const response = await api.kline(code.value, days.value)
    if (token !== serial) return
    kline.value = response
    failed.value.kline = ''
  } catch (reason) {
    if (token !== serial) return
    failed.value.kline = failText(reason, '日 K 读不到')
  } finally {
    if (token === serial) loading.value = false
  }
  await writeUrl()
}

async function load() {
  if (!/^\d{6}$/.test(code.value)) {
    pageError.value = '地址里的股票代码不是 6 位数字'
    return
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date.value)) {
    pageError.value = '日期格式应为 YYYY-MM-DD'
    return
  }
  pageError.value = ''
  const key = `${code.value}|${date.value}`
  //: 换股票、换日子就把五块清掉——留着上一只的数字比空着更害人。同一只刷新则维持上一份。
  if (key !== loadedKey) {
    loadedKey = key
    resetCards()
  }
  const token = ++serial
  loading.value = true
  const [line, matches, pattern, pick, limitups] = await Promise.allSettled([
    api.kline(code.value, days.value),
    api.storedMatches(code.value, date.value),
    api.patternDetail(code.value, false),
    api.pickHistory(code.value),
    api.limitups(date.value),
  ])
  if (token !== serial) return
  if (line.status === 'fulfilled') {
    kline.value = line.value
    failed.value.kline = ''
  } else {
    failed.value.kline = failText(line.reason, '日 K 读不到')
  }
  if (matches.status === 'fulfilled') {
    stored.value = matches.value
    failed.value.matches = ''
  } else {
    failed.value.matches = failText(matches.reason, '落库汇总读不到')
  }
  if (pattern.status === 'fulfilled') {
    patterns.value = pattern.value
    failed.value.patterns = ''
  } else if (pattern.reason instanceof ApiError && pattern.reason.status === 404) {
    //: 404 = 这只票保留窗口里没涨停过，是正常的空，不是读数坏了。
    patterns.value = null
    failed.value.patterns = ''
  } else {
    failed.value.patterns = failText(pattern.reason, '涨停样本读不到')
  }
  if (pick.status === 'fulfilled') {
    picks.value = pick.value
    failed.value.picks = ''
  } else {
    failed.value.picks = failText(pick.reason, 'AI 那笔账读不到')
  }
  if (limitups.status === 'fulfilled') {
    limitRow.value = limitups.value.items.find((item) => item.code === code.value) ?? null
    failed.value.limitups = ''
  } else {
    failed.value.limitups = failText(limitups.reason, '这一天涨停榜读不到')
  }
  loading.value = false
  await writeUrl()
}

function openSample(sampleCode: string, limitDate: string) {
  if (!/^\d{6}$/.test(sampleCode)) return
  void router.push({ name: 'stock', params: { code: sampleCode }, query: { date: limitDate } })
}

function openSimilarity() {
  if (!code.value || !date.value) return
  void router.push({ name: 'similarity', query: { code: code.value, date: date.value } })
}

onMounted(async () => {
  if (!session.health) await loadHealth()
  readRoute()
  if (!date.value) date.value = session.health?.data_date ?? ''
  await load()
})

watch(
  () => [route.params.code, route.query.date, route.query.days],
  () => {
    const before = days.value
    readRoute()
    if (`${code.value}|${date.value}` !== loadedKey) {
      void load()
      return
    }
    //: 地址里只是被自己刚写回的那串改动过 → 什么都不敲；只有 K 线根数变了 → 只重取 K 线。
    if (days.value !== before) void reloadKline()
  },
)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>{{ kline?.name || '股票详情' }} <span class="code muted">{{ code }}</span></h1>
      <p class="lede">
        <span v-if="kline?.board_label">{{ kline.board_label }} · </span>
        <span v-if="kline?.is_st">ST · </span>
        {{ date }}
      </p>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>日期</span>
        <input v-model="date" class="mono" type="text" maxlength="10" placeholder="YYYY-MM-DD" @change="load" />
      </label>
      <label class="field">
        <span>K 线根数</span>
        <select v-model.number="days" @change="reloadKline">
          <option v-for="item in DAYS" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">
        {{ loading ? '读数中…' : '刷新' }}
      </button>
      <button class="btn" type="button" @click="openSimilarity">逐日对比</button>
    </div>
  </header>

  <p v-if="pageError" class="alert">{{ pageError }}</p>

  <section class="card">
    <div class="card-head">
      <h2>日 K</h2>
      <span class="muted">
        涨停 {{ markCount }} 根<span v-if="unknownMarks"> · 未判 {{ unknownMarks }} 根</span>
      </span>
    </div>
    <p v-if="failed.kline" class="alert">{{ failed.kline }}</p>
    <div v-else-if="loading && !bars.length" class="skeleton" />
    <KlineChart v-else-if="bars.length" :bars="bars" :height="380" />
    <p v-else class="empty">这只票库里没有日线</p>
  </section>

  <section v-if="limitRow || failed.limitups" class="card">
    <div class="card-head">
      <h2>这一天它的涨停账</h2>
      <span v-if="limitRow" class="tag" :class="limitTypeClass(limitRow.limit_type)">{{ limitRow.limit_type || '—' }}</span>
    </div>
    <p v-if="failed.limitups" class="alert">{{ failed.limitups }}</p>
    <div v-else-if="limitRow" class="feature-grid">
      <div class="feature"><span class="muted">收盘</span><b>{{ formatPrice(limitRow.close) }}</b></div>
      <div class="feature"><span class="muted">涨幅</span><b class="up">{{ formatPct(limitRow.pct_chg) }}</b></div>
      <div class="feature"><span class="muted">连板</span><b>{{ limitRow.consecutive_boards }} 板</b></div>
      <div class="feature"><span class="muted">首次封板</span><b>{{ formatTime(limitRow.first_limit_time) }}</b></div>
      <div class="feature"><span class="muted">成交量</span><b>{{ formatVolume(limitRow.volume) }}</b></div>
      <div class="feature">
        <span class="muted">成交额</span>
        <b>{{ formatAmount(limitRow.amount) }}<template v-if="limitRow.amount_estimated"> 估</template></b>
      </div>
      <div class="feature"><span class="muted">换手率</span><b>{{ formatTurnover(limitRow.turnover_rate) }}</b></div>
      <div class="feature"><span class="muted">交易所</span><b>{{ limitRow.board }}</b></div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>扫描汇总</h2>
      <span class="muted">{{ stored?.pool_dates.map(shortDate).join('、') || '—' }}</span>
    </div>
    <p v-if="failed.matches" class="alert">{{ failed.matches }}</p>
    <template v-else-if="stored">
      <p v-if="!stored.ok" class="empty">{{ stored.detail || '库里这一天没有它的明细' }}</p>
      <template v-else>
        <Disclaimer :text="stored.disclaimer" />
        <div class="stats">
          <div class="card stat">
            <div class="stat-label">参与比对</div>
            <div class="stat-value">{{ summary?.sample_count ?? stored.items.length }}</div>
            <div class="stat-foot">样本池 {{ summary?.pool_size ?? stored.pool_size }} 条</div>
          </div>
          <div class="card stat">
            <div class="stat-label">过高分线</div>
            <div class="stat-value">{{ summary?.high_similarity_count ?? '—' }}</div>
            <div class="stat-foot">{{ summary ? (summary.high_similarity_rate === null ? '—' : `${summary.high_similarity_rate}%`) : '—' }}</div>
          </div>
          <div class="card stat">
            <div class="stat-label">最高相似度</div>
            <div class="stat-value" :style="{ color: scoreColor(summary?.max_similarity ?? 0) }">
              {{ formatScore(summary?.max_similarity) }}
            </div>
            <div class="stat-foot">{{ summary?.band || '—' }}</div>
          </div>
          <div class="card stat">
            <div class="stat-label">全池平均</div>
            <div class="stat-value">{{ formatScore(summary?.avg_similarity) }}</div>
            <div class="stat-foot">落库 {{ formatDateTime(summary?.created_at) }}</div>
          </div>
        </div>
      </template>
    </template>

    <div v-if="stored?.ok && stored.items.length" style="margin-top: 14px">
      <div class="card-head">
        <h3>最相似的涨停样本</h3>
        <span class="muted">前 {{ stored.items.length }} 条</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="right">#</th>
              <th>代码</th>
              <th>名称</th>
              <th>涨停日</th>
              <th class="right">综合</th>
              <th class="right">形态</th>
              <th class="right">成交量</th>
              <th class="right">量比</th>
              <th>档位</th>
              <th>形态三天</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in stored.items"
              :key="`${item.code}-${item.limit_date}`"
              tabindex="0"
              @click="openSample(item.code, item.limit_date)"
              @keydown.enter="openSample(item.code, item.limit_date)"
            >
              <td class="rank" :class="{ top: item.rank <= 3 }">{{ String(item.rank).padStart(2, '0') }}</td>
              <td class="code">{{ item.code }}</td>
              <td>{{ item.name }}</td>
              <td class="mono">{{ shortDate(item.limit_date) }}</td>
              <td class="right">
                <span class="meter">
                  <i :style="{ '--w': `${clampPercent(item.score)}%`, background: scoreColor(item.score) }" />
                </span>
                <b :style="{ color: scoreColor(item.score) }">{{ formatScore(item.score) }}</b>
              </td>
              <td class="right">{{ formatScore(item.shape_score) }}</td>
              <td class="right">{{ formatScore(item.volume_score) }}</td>
              <td class="right">{{ formatScore(item.volume_pct_score) }}</td>
              <td class="muted">{{ item.band }}</td>
              <td class="mono muted">{{ item.window.map(shortDate).join(' / ') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>它自己的涨停样本</h2>
      <span v-if="patterns" class="muted">{{ samples.length }} 条</span>
    </div>
    <p v-if="failed.patterns" class="alert">{{ failed.patterns }}</p>
    <div v-else-if="!samples.length" class="empty">保留窗口里它没有涨停样本</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>涨停日</th>
            <th>形态三天</th>
            <th class="right">收盘</th>
            <th class="right">当日涨幅</th>
            <th class="right">向量维度</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in samples" :key="item.limit_date">
            <td class="mono">{{ item.limit_date }}</td>
            <td class="mono muted">{{ item.window.map(shortDate).join(' / ') }}</td>
            <td class="right">{{ formatPrice(item.close ?? null) }}</td>
            <td class="right" :class="(item.change_percent ?? 0) > 0 ? 'up' : 'flat'">
              {{ formatPct(item.change_percent ?? null) }}
            </td>
            <td class="right">{{ item.dimension }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>模型对它说过的话</h2>
      <span v-if="history" class="muted mono">{{ history.formula }}</span>
    </div>
    <p v-if="failed.picks" class="alert">{{ failed.picks }}</p>
    <p v-else-if="picks?.never_recommended" class="empty">模型没把它写进过推荐</p>
    <template v-else-if="history">
      <div class="chips" style="margin-bottom: 12px">
        <span class="chip">推荐 {{ history.picks }} 次</span>
        <span class="chip">定判 {{ history.settled }} 次</span>
        <span class="chip">中 {{ history.hits }} · 不中 {{ history.misses }} · 待验证 {{ history.pending }}</span>
        <span class="chip">命中率 {{ history.hit_rate_percent === null ? '—' : `${history.hit_rate_percent}%` }}</span>
        <span class="chip">自评均分 {{ formatScore(history.avg_score) }}</span>
        <span class="chip">历史分 {{ history.history_score === null ? '—' : history.history_score }}</span>
      </div>
      <div class="two">
        <div class="readonly">
          <div><span class="muted">最近一次推荐</span> <b>{{ formatDateTime(history.latest.recommended_at) }}</b></div>
          <div>
            <span class="muted">判定</span>
            <span class="tag" :class="verdictClass(history.latest.verdict)">{{ verdictLabel(history.latest.verdict) }}</span>
          </div>
          <div><span class="muted">定判时刻</span> <b>{{ history.latest.checked_at ? formatDateTime(history.latest.checked_at) : '还没对过账' }}</b></div>
          <div><span class="muted">模型</span> <b>{{ history.latest.model || '—' }}</b></div>
        </div>
        <div class="readonly">
          <div>
            <span class="muted">自评 / 综合 / 档位</span>
            <b>{{ formatScore(history.latest.score) }} · {{ formatScore(history.latest.composite_score) }} · {{ history.latest.level || '—' }}</b>
          </div>
          <div class="wrap"><span class="muted">理由</span> <b>{{ history.latest.reason || '—' }}</b></div>
          <div class="wrap"><span class="muted">依据</span> <b>{{ history.latest.criteria || '—' }}</b></div>
        </div>
      </div>
    </template>
  </section>
</template>
