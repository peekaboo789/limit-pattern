<script setup lang="ts">
import { api } from '@/api/client'
import type { AiRanking, MonitorSnapshot } from '@/api/types'
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'

const ranking = ref<AiRanking | null>(null)
const snap = ref<MonitorSnapshot | null>(null)
let timer: number | undefined

// 报警与否只听后端的统一判断（交易日历 + 交易时段 + 「仅在交易时段报警」）
const active = computed(() => snap.value?.alerts_active === true)
const hot = computed(() => (active.value ? (snap.value?.items ?? []).filter((item) => item.level !== 'normal') : []))
const hasAlert = computed(() => active.value && hot.value.some((item) => item.level === 'alert'))
// 休市时只静态列出最后行情（榜单顺序前 5），不用报警样式
const staticRows = computed(() => (active.value ? [] : (snap.value?.items ?? []).slice(0, 5)))
const first = computed(() => ranking.value?.items[0])
const firstText = computed(() => {
  const row = first.value
  if (!row) return '—'
  const composite = row.composite_score ?? row.ai_score
  if (ranking.value?.trigger === 'external' && row.model_score != null)
    return `${row.name}（外部AI ${row.model_score} · 综合 ${composite ?? '—'}）`
  return `${row.name}（综合 ${composite ?? '—'}）`
})

async function load() {
  snap.value = await api.monitor().catch(() => null)
}
async function refresh() {
  ranking.value = await api.aiRanking().catch(() => null)
  await load()
}
function startTimer() {
  if (timer === undefined) timer = window.setInterval(load, 15000)
}
function stopTimer() {
  if (timer !== undefined) window.clearInterval(timer)
  timer = undefined
}

onMounted(async () => {
  await refresh()
  startTimer()
})
onActivated(() => {
  void refresh()
  startTimer()
})
onDeactivated(stopTimer)
onUnmounted(stopTimer)
</script>

<template>
  <section class="card monitor-panel" :class="{ 'panel-alert': hasAlert }">
    <div class="card-head">
      <h2>AI 榜单 · 实时监控</h2>
      <span class="toolbar">
        <span class="market-chip" :class="{ on: active && snap?.market_open }">{{ active && snap?.market_open ? '交易中' : '休市' }}</span>
        <RouterLink class="btn btn-ghost" to="/ai-ranking">AI 榜单</RouterLink>
        <RouterLink class="btn btn-ghost" to="/monitor">实时监控</RouterLink>
      </span>
    </div>
    <p class="muted" style="margin: 0 0 8px">
      <template v-if="ranking">最新榜单 {{ ranking.date }}，第一名 {{ firstText }}。</template>
      <template v-else>还没有 AI 榜单。</template>
      <template v-if="snap?.ranking_date && active"> 正在监控 {{ snap.ranking_date }} 的前 20 名：超 3% {{ snap.warn_count ?? 0 }} 只，超 6% {{ snap.alert_count ?? 0 }} 只。</template>
      <template v-else-if="snap?.ranking_date"> {{ snap.status_text }}；监控 {{ snap.ranking_date }} 榜单，下面是{{ snap.price_label ?? '最后行情' }}。</template>
    </p>
    <div v-for="item in hot.slice(0, 5)" :key="item.code" class="overview-ai-row">
      <span>#{{ item.rank }} {{ item.name }} <span class="code muted">{{ item.code }}</span></span>
      <b :class="item.level === 'alert' ? 'up' : ''">{{ item.pct?.toFixed(2) }}%</b>
    </div>
    <div v-for="item in staticRows" :key="item.code" class="overview-ai-row static">
      <span>#{{ item.rank }} {{ item.name }} <span class="code muted">{{ item.code }}</span></span>
      <span class="muted">{{ item.pct == null ? '—' : `${item.pct >= 0 ? '+' : ''}${item.pct.toFixed(2)}%` }}</span>
    </div>
  </section>
</template>
