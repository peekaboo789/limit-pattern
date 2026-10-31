<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { CalendarInfo, MonitorSnapshot } from '@/api/types'
import { monitorState, setSound, testAlerts } from '@/stores/monitor'
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'

const snap = ref<MonitorSnapshot | null>(null)
const calendar = ref<CalendarInfo | null>(null)
const error = ref('')
let timer: number | undefined

// 红框只在报警时段亮；收盘/午休/非交易日自动熄灭（测试提醒的演示除外）
const alertsActive = computed(() => snap.value?.alerts_active !== false)
const hasAlert = computed(
  () => monitorState.demoAlert || (alertsActive.value && (snap.value?.items.some((item) => item.level === 'alert') ?? false)),
)
const chipText = computed(() => (snap.value ? snap.value.status_text || (snap.value.market_open ? '交易中' : '休市') : '…'))

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
  void api.calendar().then((value) => (calendar.value = value)).catch(() => undefined)
  void load()
  timer = window.setInterval(load, 5000)
})
onActivated(() => {
  if (timer === undefined) {
    void load()
    timer = window.setInterval(load, 5000)
  }
})
onDeactivated(() => {
  if (timer !== undefined) window.clearInterval(timer)
  timer = undefined
})
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>实时监控</h1>
      <p class="lede">盯住上一交易日 AI 前 20 名。涨幅超 3% 弹窗提醒，超 6% 整框变红；只在交易时段（9:30–11:30、13:00–15:00）报警。每 5 秒刷新。</p>
    </div>
    <div class="toolbar">
      <span class="market-chip" :class="{ on: alertsActive && snap?.market_open }">{{ chipText }}</span>
      <label class="muted"><input type="checkbox" :checked="monitorState.sound" @change="setSound(($event.target as HTMLInputElement).checked)" /> 提示音</label>
      <button class="btn btn-ghost" type="button" @click="testAlerts()">测试提醒</button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <p v-if="monitorState.testResult" class="muted" style="margin: 0">{{ monitorState.testResult }}<template v-if="monitorState.demoAlert">（红框演示中，10 秒后恢复）</template></p>
  <p v-if="calendar" class="muted" style="margin: 0">
    交易日历：今天 {{ calendar.today }} {{ calendar.today_is_trading_day ? '是交易日' : '休市' }} · 上一交易日 {{ calendar.previous_trading_day }} · 下一交易日 {{ calendar.next_trading_day }}
    <template v-if="calendar.estimated">（{{ calendar.year }} 年休市安排为推算，公告后请核对）</template>
  </p>
  <section class="card monitor-panel" :class="{ 'panel-alert': hasAlert }">
    <div class="card-head">
      <h2>{{ snap?.ranking_date ? `${snap.ranking_date} 的 AI 前 20 名` : '上一交易日 AI 前 20 名' }}</h2>
      <span class="muted" v-if="snap">
        <template v-if="!alertsActive">{{ snap.status_text }} · {{ snap.price_label ?? '显示最后行情' }} · </template>
        <template v-if="snap.source">行情 {{ snap.source }} · </template>
        更新 {{ snap.updated_at.slice(11, 19) }}
      </span>
    </div>
    <p v-if="snap?.error" class="muted">{{ snap.error }}</p>
    <div v-if="!snap" class="skeleton" />
    <div v-else-if="!snap.items.length" class="empty">还没有上一交易日的 AI 榜单，生成后第二个交易日开始监控</div>
    <div v-else class="monitor-grid">
      <article v-for="item in snap.items" :key="item.code" class="monitor-card" :class="alertsActive ? item.level : 'normal'">
        <div class="match-top">
          <span class="rank">#{{ item.rank }}</span>
          <strong>{{ item.name }}</strong>
          <span class="code muted">{{ item.code }}</span>
        </div>
        <div class="monitor-pct" :class="item.pct == null ? 'flat' : item.pct >= 0 ? 'up' : 'down'">{{ pctText(item.pct) }}</div>
        <div class="muted">现价 {{ item.price ?? '—' }} · 综合 {{ item.ai_score ?? "—" }}</div>
      </article>
    </div>
  </section>
</template>
