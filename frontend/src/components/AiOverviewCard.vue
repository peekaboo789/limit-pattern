<script setup lang="ts">
import { api } from '@/api/client'
import type { AiRanking, MonitorSnapshot } from '@/api/types'
import { computed, onMounted, onUnmounted, ref } from 'vue'

const ranking = ref<AiRanking | null>(null)
const snap = ref<MonitorSnapshot | null>(null)
let timer: number | undefined

const hot = computed(() => (snap.value?.items ?? []).filter((item) => item.level !== 'normal'))
const hasAlert = computed(() => hot.value.some((item) => item.level === 'alert'))

async function load() {
  snap.value = await api.monitor().catch(() => null)
}

onMounted(async () => {
  ranking.value = await api.aiRanking().catch(() => null)
  await load()
  timer = window.setInterval(load, 15000)
})
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <section class="card monitor-panel" :class="{ 'panel-alert': hasAlert }">
    <div class="card-head">
      <h2>AI 榜单 · 实时监控</h2>
      <span class="toolbar">
        <span class="market-chip" :class="{ on: snap?.market_open }">{{ snap?.market_open ? '交易中' : '休市' }}</span>
        <RouterLink class="btn btn-ghost" to="/ai-ranking">AI 榜单</RouterLink>
        <RouterLink class="btn btn-ghost" to="/monitor">实时监控</RouterLink>
      </span>
    </div>
    <p class="muted" style="margin: 0 0 8px">
      <template v-if="ranking">最新榜单 {{ ranking.date }}，第一名 {{ ranking.items[0]?.name ?? '—' }}（综合 {{ ranking.items[0]?.composite_score ?? ranking.items[0]?.ai_score ?? '—' }}）。</template>
      <template v-else>还没有 AI 榜单。</template>
      <template v-if="snap?.ranking_date"> 正在监控 {{ snap.ranking_date }} 的前 20 名：超 3% {{ snap.warn_count ?? 0 }} 只，超 6% {{ snap.alert_count ?? 0 }} 只。</template>
    </p>
    <div v-for="item in hot.slice(0, 5)" :key="item.code" class="overview-ai-row">
      <span>#{{ item.rank }} {{ item.name }} <span class="code muted">{{ item.code }}</span></span>
      <b :class="item.level === 'alert' ? 'up' : ''">{{ item.pct?.toFixed(2) }}%</b>
    </div>
  </section>
</template>
