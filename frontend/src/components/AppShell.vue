<script setup lang="ts">
import { initSession, session } from '@/stores/session'
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const mockMode = import.meta.env.VITE_USE_MOCK === '1'

const items = [
  { name: 'overview', label: '总览', to: '/' },
  { name: 'similarity', label: '个股相似度', to: '/similarity' },
  { name: 'pool', label: '涨停池', to: '/pool' },
  { name: 'summaries', label: '每日总结', to: '/summaries' },
  { name: 'settings', label: '设置', to: '/settings' },
]

const syncText = computed(() => {
  if (session.sync?.running) return `同步中 ${Math.round(session.sync.progress)}%`
  if (session.sync?.message) return session.sync.message
  return '行情待命'
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
        :class="{ active: route.name === item.name }"
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
