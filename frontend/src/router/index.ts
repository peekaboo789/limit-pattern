import AiRankView from '@/views/AiRankView.vue'
import BacktestView from '@/views/BacktestView.vue'
import ClustersView from '@/views/ClustersView.vue'
import DataView from '@/views/DataView.vue'
import OverviewView from '@/views/OverviewView.vue'
import PatternsView from '@/views/PatternsView.vue'
import PoolView from '@/views/PoolView.vue'
import PredictionView from '@/views/PredictionView.vue'
import RealtimeView from '@/views/RealtimeView.vue'
import SettingsView from '@/views/SettingsView.vue'
import SimilarityView from '@/views/SimilarityView.vue'
import StockDetailView from '@/views/StockDetailView.vue'
import StocksView from '@/views/StocksView.vue'
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
    { path: '/stocks', name: 'stocks', component: StocksView, meta: { title: '股票列表' } },
    { path: '/stocks/:code', name: 'stock', component: StockDetailView, meta: { title: '股票详情' } },
    { path: '/pool', name: 'pool', component: PoolView, meta: { title: '涨停池' } },
    { path: '/prediction', name: 'prediction', component: PredictionView, meta: { title: '次日预测池' } },
    { path: '/ai-rank', name: 'ai-rank', component: AiRankView, meta: { title: '每日 AI 固定榜' } },
    { path: '/summaries', name: 'summaries', component: SummaryView, meta: { title: '每日总结' } },
    { path: '/backtest', name: 'backtest', component: BacktestView, meta: { title: '历史回测' } },
    { path: '/clusters', name: 'clusters', component: ClustersView, meta: { title: '形态聚类' } },
    { path: '/patterns', name: 'patterns', component: PatternsView, meta: { title: '涨停形态样本' } },
    { path: '/data', name: 'data', component: DataView, meta: { title: '数据管理' } },
    { path: '/settings', name: 'settings', component: SettingsView, meta: { title: '设置' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : '三日涨停形态'
  document.title = `${title} · 三日涨停形态统计`
})
