import AiRankView from '@/views/AiRankView.vue'
import OverviewView from '@/views/OverviewView.vue'
import PoolView from '@/views/PoolView.vue'
import RealtimeView from '@/views/RealtimeView.vue'
import SettingsView from '@/views/SettingsView.vue'
import SimilarityView from '@/views/SimilarityView.vue'
import SummaryView from '@/views/SummaryView.vue'
import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  scrollBehavior() {
    return { top: 0 }
  },
  routes: [
    { path: '/', name: 'overview', component: OverviewView, meta: { title: '总览' } },
    { path: '/realtime', name: 'realtime', component: RealtimeView, meta: { title: '盘中监控' } },
    { path: '/similarity', name: 'similarity', component: SimilarityView, meta: { title: '个股相似度' } },
    { path: '/pool', name: 'pool', component: PoolView, meta: { title: '涨停池' } },
    { path: '/ai-rank', name: 'ai-rank', component: AiRankView, meta: { title: '每日 AI 固定榜' } },
    { path: '/summaries', name: 'summaries', component: SummaryView, meta: { title: '每日总结' } },
    { path: '/settings', name: 'settings', component: SettingsView, meta: { title: '设置' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : '三日涨停形态'
  document.title = `${title} · 三日涨停形态统计`
})
