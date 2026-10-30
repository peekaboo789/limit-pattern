<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { MonitorSnapshot } from '@/api/types'
import { monitorState, setSound } from '@/stores/monitor'
import { computed, onMounted, onUnmounted, ref } from 'vue'

const snap = ref<MonitorSnapshot | null>(null)
const error = ref('')
let timer: number | undefined

const hasAlert = computed(() => snap.value?.items.some((item) => item.level === 'alert') ?? false)

async function load() {
  try {
    snap.value = await api.monitor()
    error.value = ''
  } catch (reason) {
    if (!isAbort(reason)) error.value = reason instanceof ApiError ? reason.detail : '监控数据加载失败'
  }
}

function pctText(value: number | null) {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

onMounted(() => {
  void load()
  timer = window.setInterval(load, 5000)
})
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>实时监控</h1>
      <p class="lede">盯住上一交易日 AI 前 20 名。涨幅超 3% 弹窗提醒，超 6% 整框变红。每 5 秒刷新。</p>
    </div>
    <div class="toolbar">
      <span class="market-chip" :class="{ on: snap?.market_open }">{{ snap ? (snap.market_open ? '交易中' : '休市') : '…' }}</span>
      <label class="muted"><input type="checkbox" :checked="monitorState.sound" @change="setSound(($event.target as HTMLInputElement).checked)" /> 提示音</label>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <section class="card monitor-panel" :class="{ 'panel-alert': hasAlert }">
    <div class="card-head">
      <h2>{{ snap?.ranking_date ? `${snap.ranking_date} 的 AI 前 20 名` : '上一交易日 AI 前 20 名' }}</h2>
      <span class="muted" v-if="snap">
        <template v-if="!snap.market_open">休市 · </template>
        <template v-if="snap.source">行情 {{ snap.source }} · </template>
        更新 {{ snap.updated_at.slice(11, 19) }}
      </span>
    </div>
    <p v-if="snap?.error" class="muted">{{ snap.error }}</p>
    <div v-if="!snap" class="skeleton" />
    <div v-else-if="!snap.items.length" class="empty">还没有上一交易日的 AI 榜单，生成后第二个交易日开始监控</div>
    <div v-else class="monitor-grid">
      <article v-for="item in snap.items" :key="item.code" class="monitor-card" :class="item.level">
        <div class="match-top">
          <span class="rank">#{{ item.rank }}</span>
          <strong>{{ item.name }}</strong>
          <span class="code muted">{{ item.code }}</span>
        </div>
        <div class="monitor-pct" :class="item.pct == null ? 'flat' : item.pct >= 0 ? 'up' : 'down'">{{ pctText(item.pct) }}</div>
        <div class="muted">现价 {{ item.price ?? '—' }} · AI {{ item.ai_score ?? '—' }}</div>
      </article>
    </div>
  </section>
</template>
