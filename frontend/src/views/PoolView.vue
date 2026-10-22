<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import ExportButton from '@/components/ExportButton.vue'
import type { ExportParams, PoolItem } from '@/api/types'
import { boardLabel, formatAmount, formatPct, formatPrice, formatTime, formatTurnover, pctClass, shortDate } from '@/utils/format'
import { limitTypeClass } from '@/utils/labels'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const dates = ref<string[]>([])
const items = ref<PoolItem[]>([])
const day = ref('all')
const board = ref('all')
const kind = ref('all')
const keyword = ref('')
const minBoards = ref(1)
const loading = ref(false)
const error = ref('')

const kinds = computed(() => [...new Set(items.value.map((item) => item.limit_type))])
const exportParams = computed<ExportParams | null>(() => {
  const date = day.value !== 'all' ? day.value : dates.value[dates.value.length - 1]
  if (!date) return null
  return { type: 'pool', date }
})

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  return items.value.filter((item) => {
    if (day.value !== 'all' && item.limit_date !== day.value) return false
    if (board.value !== 'all' && item.board !== board.value) return false
    if (kind.value !== 'all' && item.limit_type !== kind.value) return false
    if (item.consecutive_boards < minBoards.value) return false
    if (text && !`${item.code}${item.name}`.toLowerCase().includes(text)) return false
    return true
  })
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const pool = await api.pool()
    dates.value = pool.dates
    items.value = pool.items
  } catch (reason) {
    if (isAbort(reason)) return
    error.value = reason instanceof ApiError ? reason.detail : '涨停池加载失败'
  } finally {
    loading.value = false
  }
}

function open(item: PoolItem) {
  void router.push({ name: 'similarity', query: { code: item.code, date: item.limit_date } })
}

onMounted(() => {
  void load()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>涨停池</h1>
      <p class="lede">只列出最近三个交易日里涨停过的沪深股票。点一行进入相似度对比。</p>
    </div>
    <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">刷新</button>
  </header>

  <section class="card">
    <div class="filters">
      <div class="chips">
        <button class="chip" :class="{ active: day === 'all' }" type="button" @click="day = 'all'">全部 {{ items.length }}</button>
        <button
          v-for="date in dates"
          :key="date"
          class="chip"
          :class="{ active: day === date }"
          type="button"
          @click="day = date"
        >
          {{ shortDate(date) }} {{ items.filter((item) => item.limit_date === date).length }}
        </button>
      </div>
      <label class="field">
        <span>市场</span>
        <select v-model="board">
          <option value="all">全部</option>
          <option value="SH">沪市</option>
          <option value="SZ">深市</option>
        </select>
      </label>
      <label class="field">
        <span>形态</span>
        <select v-model="kind">
          <option value="all">全部</option>
          <option v-for="item in kinds" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="field">
        <span>最少连板</span>
        <select v-model.number="minBoards">
          <option :value="1">1 板起</option>
          <option :value="2">2 板起</option>
          <option :value="3">3 板起</option>
        </select>
      </label>
      <label class="field">
        <span>代码 / 名称</span>
        <input v-model="keyword" placeholder="过滤" />
      </label>
    </div>
  </section>

  <p v-if="error" class="alert">{{ error }}</p>
  <div v-else-if="loading" class="skeleton" />
  <section v-else class="card">
    <div class="card-head">
      <h2>涨停明细</h2>
      <div class="toolbar">
        <span class="muted">显示 {{ filtered.length }} / {{ items.length }}</span>
        <ExportButton :params="exportParams" />
      </div>
    </div>
    <div v-if="!filtered.length" class="empty">这个筛选条件下没有涨停</div>
    <div v-else class="table-wrap" style="max-height: calc(100vh - 280px)">
      <table>
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>市场</th>
            <th>涨停日</th>
            <th class="right">收盘</th>
            <th class="right">涨幅</th>
            <th>连板</th>
            <th>封板</th>
            <th>形态</th>
            <th class="right">换手</th>
            <th class="right">成交额</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in filtered" :key="item.code + item.limit_date" tabindex="0" @click="open(item)" @keydown.enter="open(item)">
            <td class="code">{{ item.code }}</td>
            <td>{{ item.name }}</td>
            <td><span class="pill" :class="item.board === 'SH' ? 'pill-sh' : 'pill-sz'">{{ boardLabel(item.board) }}</span></td>
            <td>{{ item.limit_date }}</td>
            <td class="right">{{ formatPrice(item.close) }}</td>
            <td class="right" :class="pctClass(item.pct_chg)">{{ formatPct(item.pct_chg) }}</td>
            <td><span class="tag" :class="{ 'tag-hot': item.consecutive_boards >= 3 }">{{ item.consecutive_boards }} 板</span></td>
            <td>{{ formatTime(item.first_limit_time) }}</td>
            <td><span class="tag" :class="limitTypeClass(item.limit_type)">{{ item.limit_type }}</span></td>
            <td class="right">{{ formatTurnover(item.turnover_rate) }}</td>
            <td class="right">{{ formatAmount(item.amount) }}<template v-if="item.amount_estimated"> 估</template></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
