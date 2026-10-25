<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { MatchSummaryPage } from '@/api/types'
import { loadHealth, session } from '@/stores/session'
import { clampPercent, formatDateTime, formatScore, scoreColor } from '@/utils/format'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const date = ref('')
const keyword = ref('')
const board = ref('')
const sort = ref('max')
const order = ref('desc')
const limit = ref(100)
const offset = ref(0)

const page = ref<MatchSummaryPage | null>(null)
const loading = ref(false)
const error = ref('')
let serial = 0

const LIMITS = [50, 100, 200, 500]
const DEFAULTS = { sort: 'max', order: 'desc', limit: 100 }

const total = computed(() => page.value?.total ?? 0)
const first = computed(() => (total.value ? offset.value + 1 : 0))
const last = computed(() => Math.min(offset.value + limit.value, total.value))
const canPrev = computed(() => offset.value > 0)
const canNext = computed(() => last.value < total.value)
/** 排序下拉与板块下拉都只读回包：那两份档位是后端 `SUMMARY_SORTS` 与 `stocks.board` 的原文，
 *  前端自己写一份迟早和接口漂开（非法键的 422 文案也就对不上了）。 */
const sortOptions = computed(() => page.value?.sort_options ?? [])
const boardRows = computed(() => page.value?.boards ?? [])

function wanted(): Record<string, string> {
  const text: Record<string, string> = {}
  if (date.value) text.date = date.value
  const q = keyword.value.trim()
  if (q) text.q = q
  if (board.value) text.board = board.value
  if (sort.value !== DEFAULTS.sort) text.sort = sort.value
  if (order.value !== DEFAULTS.order) text.order = order.value
  if (limit.value !== DEFAULTS.limit) text.limit = String(limit.value)
  if (offset.value) text.offset = String(offset.value)
  return text
}

function serialize(text: Record<string, string>): string {
  return Object.keys(text)
    .sort()
    .map((key) => `${key}=${text[key] ?? ''}`)
    .join('&')
}

function routeText(): Record<string, string> {
  const text: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (typeof value === 'string') text[key] = value
  }
  return text
}

/** 地址栏那串是唯一一份状态：谁改了控件就把这串重写，前进后退再把串读回控件。
 *  两边都比同一把尺子（排序后的键值），所以不会自己写自己、也不会来回多敲一次接口。 */
async function writeUrl() {
  const text = wanted()
  if (serialize(text) === serialize(routeText())) return
  await router.replace({ name: 'stocks', query: text })
}

function adoptFromRoute(): boolean {
  const text = routeText()
  const next = {
    date: text.date ?? '',
    q: text.q ?? '',
    board: text.board ?? '',
    sort: text.sort ?? DEFAULTS.sort,
    order: text.order ?? DEFAULTS.order,
    limit: Number(text.limit ?? DEFAULTS.limit),
    offset: Number(text.offset ?? 0),
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(next.date)) next.date = date.value
  if (!LIMITS.includes(next.limit)) next.limit = limit.value
  if (!Number.isFinite(next.offset) || next.offset < 0) next.offset = offset.value
  if (!['desc', 'asc'].includes(next.order)) next.order = order.value
  const changed =
    next.date !== date.value ||
    next.q !== keyword.value ||
    next.board !== board.value ||
    next.sort !== sort.value ||
    next.order !== order.value ||
    next.limit !== limit.value ||
    next.offset !== offset.value
  if (!changed) return false
  date.value = next.date
  keyword.value = next.q
  board.value = next.board
  sort.value = next.sort
  order.value = next.order
  limit.value = next.limit
  offset.value = next.offset
  return true
}

async function reload(writeUrlAfter = true) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date.value)) {
    error.value = '日期格式应为 YYYY-MM-DD'
    return
  }
  const token = ++serial
  loading.value = true
  error.value = ''
  try {
    const result = await api.matchesSummary({
      date: date.value,
      q: keyword.value.trim(),
      board: board.value,
      sort: sort.value,
      order: order.value,
      limit: limit.value,
      offset: offset.value,
    })
    if (token !== serial) return
    if (result.total > 0 && offset.value >= result.total) {
      //: 手敲的 offset、或那天数据重算后变少：翻到最后一页，别摆一张「第 101–7 条」的空表。
      offset.value = Math.floor((result.total - 1) / limit.value) * limit.value
      loading.value = false
      return reload(writeUrlAfter)
    }
    page.value = result
    error.value = ''
  } catch (reason) {
    if (token !== serial) return
    //: 读不到维持上一份，别让一次接口失败把整页数字顶空。
    error.value = reason instanceof ApiError ? reason.detail : '这一天的汇总读不到'
  } finally {
    if (token === serial) loading.value = false
  }
  if (writeUrlAfter && token === serial) await writeUrl()
}

function filterChanged() {
  offset.value = 0
  void reload()
}

function pageBy(step: number) {
  const next = offset.value + step
  if (next < 0) return
  offset.value = next
  void reload()
}

function open(code: string) {
  if (!code) return
  void router.push({ name: 'stock', params: { code }, query: { date: page.value?.trade_date || date.value } })
}

onMounted(async () => {
  if (!session.health) await loadHealth()
  if (!date.value) date.value = session.health?.data_date ?? ''
  adoptFromRoute()
  await reload()
})

watch(
  () => route.query,
  () => {
    if (adoptFromRoute()) void reload(false)
  },
)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>股票列表</h1>
      <p class="lede">这一天全市场扫描落库的汇总，一行一只股票。</p>
    </div>
    <div class="toolbar">
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="reload()">
        {{ loading ? '读数中…' : '刷新' }}
      </button>
    </div>
  </header>

  <section class="card">
    <div class="toolbar">
      <label class="field">
        <span>扫描日期</span>
        <input v-model="date" class="mono" type="text" maxlength="10" placeholder="YYYY-MM-DD" @change="filterChanged" />
      </label>
      <label class="field" style="min-width: 190px">
        <span>代码或名称</span>
        <input v-model="keyword" class="mono" type="search" placeholder="子串匹配" @change="filterChanged" />
      </label>
      <label class="field">
        <span>板块</span>
        <select v-model="board" @change="filterChanged">
          <option value="">全部</option>
          <option v-for="item in boardRows" :key="item.board" :value="item.board">
            {{ item.label || '没登记板块' }}（{{ item.count }}）
          </option>
        </select>
      </label>
      <label class="field">
        <span>排序</span>
        <select v-model="sort" @change="filterChanged">
          <option v-for="item in sortOptions" :key="item.key" :value="item.key">{{ item.label }}</option>
        </select>
      </label>
      <label class="field">
        <span>方向</span>
        <select v-model="order" @change="filterChanged">
          <option value="desc">由高到低</option>
          <option value="asc">由低到高</option>
        </select>
      </label>
      <label class="field">
        <span>每页</span>
        <select v-model.number="limit" @change="filterChanged">
          <option v-for="item in LIMITS" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
    </div>
  </section>

  <p v-if="error" class="alert">{{ error }}</p>

  <section class="card">
    <div class="card-head">
      <h2>汇总</h2>
      <div class="toolbar">
        <span v-if="page" class="muted">
          {{ page.trade_date }} · 第 {{ first }}–{{ last }} 条 / 共 {{ total }} 条
        </span>
        <button class="btn btn-ghost" type="button" :disabled="loading || !canPrev" @click="pageBy(-limit)">上一页</button>
        <button class="btn btn-ghost" type="button" :disabled="loading || !canNext" @click="pageBy(limit)">下一页</button>
      </div>
    </div>
    <div v-if="loading && !page" class="skeleton" />
    <div v-else-if="!page || !page.items.length" class="empty">这一天下不来任何一条汇总</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>板块</th>
            <th class="right">参与比对</th>
            <th class="right">过线</th>
            <th class="right">过线比例</th>
            <th class="right">最高相似度</th>
            <th class="right">全池平均</th>
            <th>档位</th>
            <th class="right">落库时刻</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in page.items"
            :key="row.code"
            tabindex="0"
            @click="open(row.code)"
            @keydown.enter="open(row.code)"
          >
            <td class="code">{{ row.code }}</td>
            <td>{{ row.name || '—' }}</td>
            <td class="muted">{{ row.board_label || '—' }}</td>
            <td class="right">{{ row.sample_count }}</td>
            <td class="right">{{ row.high_similarity_count }}</td>
            <td class="right">{{ row.high_similarity_rate === null ? '—' : `${row.high_similarity_rate}%` }}</td>
            <td class="right">
              <span class="meter">
                <i :style="{ '--w': `${clampPercent(row.max_similarity ?? 0)}%`, background: scoreColor(row.max_similarity ?? 0) }" />
              </span>
              <b :style="{ color: scoreColor(row.max_similarity ?? 0) }">{{ formatScore(row.max_similarity) }}</b>
            </td>
            <td class="right">{{ formatScore(row.avg_similarity) }}</td>
            <td class="muted">{{ row.band || '—' }}</td>
            <td class="right muted">{{ formatDateTime(row.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
