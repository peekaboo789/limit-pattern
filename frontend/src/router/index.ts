import ClusterView from '@/views/ClusterView.vue'
import BacktestView from '@/views/BacktestView.vue'
import NextDayPoolView from '@/views/NextDayPoolView.vue'
import AiRankingView from '@/views/AiRankingView.vue'
import MonitorView from '@/views/MonitorView.vue'
import OverviewView from '@/views/OverviewView.vue'
import PoolView from '@/views/PoolView.vue'
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
    { path: '/similarity', name: 'similarity', component: SimilarityView, meta: { title: '个股相似度' } },
    { path: '/ai-ranking', name: 'ai-ranking', component: AiRankingView, meta: { title: 'AI 榜单' } },
    { path: '/monitor', name: 'monitor', component: MonitorView, meta: { title: '实时监控' } },
    { path: '/next-day', name: 'next-day', component: NextDayPoolView, meta: { title: '次日观察池' } },
    { path: '/cluster', name: 'cluster', component: ClusterView, meta: { title: '聚类分析' } },
    { path: '/backtest', name: 'backtest', component: BacktestView, meta: { title: '回测' } },
    { path: '/pool', name: 'pool', component: PoolView, meta: { title: '涨停池' } },
    { path: '/summaries', name: 'summaries', component: SummaryView, meta: { title: '每日总结' } },
    { path: '/settings', name: 'settings', component: SettingsView, meta: { title: '设置' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : '三日涨停形态'
  document.title = `${title} · 三日涨停形态统计`
})
