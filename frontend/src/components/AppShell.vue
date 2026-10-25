<script setup lang="ts">
import { initSession, session } from '@/stores/session'
import { sourceLabel } from '@/utils/labels'
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const mockMode = import.meta.env.VITE_USE_MOCK === '1'

const items: { name: string; label: string; to: string; also?: string[] }[] = [
  { name: 'overview', label: '总览', to: '/' },
  { name: 'realtime', label: '盘中监控', to: '/realtime' },
  { name: 'similarity', label: '个股相似度', to: '/similarity' },
  { name: 'stocks', label: '股票列表', to: '/stocks', also: ['stock'] },
  { name: 'pool', label: '涨停池', to: '/pool' },
  { name: 'prediction', label: '次日预测池', to: '/prediction' },
  { name: 'ai-rank', label: '每日 AI 固定榜', to: '/ai-rank' },
  { name: 'summaries', label: '每日总结', to: '/summaries' },
  { name: 'backtest', label: '历史回测', to: '/backtest' },
  { name: 'patterns', label: '涨停形态样本', to: '/patterns' },
  { name: 'clusters', label: '形态聚类', to: '/clusters' },
  { name: 'data', label: '数据管理', to: '/data' },
  { name: 'settings', label: '设置', to: '/settings' },
]

/** 详情页是从列表页点进去的，侧栏得继续亮在「股票列表」那一格。 */
function isActive(item: { name: string; also?: string[] }): boolean {
  const names = [item.name, ...(item.also ?? [])]
  return names.includes(String(route.name ?? ''))
}

const syncText = computed(() => {
  const sync = session.sync
  if (sync?.running) {
    const count = sync.total ? ` ${sync.done ?? 0}/${sync.total}` : ''
    return `同步中 ${Math.round(sync.progress)}%${count}`
  }
  if (sync?.message) return sync.message
  return '行情待命'
})

const sourceText = computed(() => {
  const active = session.health?.active_source || session.sync?.active_source
  const configured = session.health?.data_source
  if (active) return sourceLabel(active)
  if (configured) return sourceLabel(configured)
  return ''
})

onMounted(() => {
  void initSession()
})
</script>

<template>
  <div class="shell">
    <aside class="nav">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"><span /><span /><span /></div>
        <div>
          <div class="brand-name">涨停形态</div>
          <div class="brand-sub">三日窗口 · 沪深</div>
        </div>
      </div>
      <RouterLink
        v-for="item in items"
        :key="item.name"
        class="nav-link"
        :class="{ active: isActive(item) }"
        :to="item.to"
      >
        {{ item.label }}
      </RouterLink>
      <div class="nav-foot">只使用已发生的行情<br />不预测下一交易日</div>
    </aside>
    <div class="workspace">
      <div class="workspace-head">
        <header class="topbar">
          <div class="date-pill">
            <span class="muted">数据日期</span>
            <strong>{{ session.health?.data_date ?? '—' }}</strong>
          </div>
          <div class="sync-chip" :class="{ on: session.sync?.running }">
            <i class="dot" />
            <span>{{ syncText }}</span>
          </div>
          <span v-if="sourceText" class="muted">{{ sourceText }}</span>
          <span v-if="mockMode" class="mock-badge">模拟数据</span>
          <span class="top-spacer" />
          <span class="version">{{ session.health?.version ?? '' }}</span>
        </header>
        <div class="disclaimer-bar" role="note">
          <span class="mark">声明</span>
          <strong>相似度不是涨停概率</strong>
          <span class="sub">只衡量与最近三个交易日涨停样本的形态接近程度，不预测之后涨跌。</span>
        </div>
      </div>
      <main class="content">
        <div v-if="session.healthError" class="alert">{{ session.healthError }}</div>
        <slot />
      </main>
    </div>
  </div>
</template>
