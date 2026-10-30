<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { ClusterResult, ExportParams } from '@/api/types'
import ClusterPattern from '@/components/ClusterPattern.vue'
import ClusterScatter from '@/components/ClusterScatter.vue'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import { computed, onMounted, ref } from 'vue'

const COLORS = ['#5ee0d0', '#ff5d73', '#e8b86d', '#7eb0ff', '#b98bff', '#2fdb9a']
const result = ref<ClusterResult | null>(null)
const available = ref<boolean | null>(null)
const missingMessage = ref('')
const k = ref<number | 'auto'>('auto')
const method = ref('kmeans')
const running = ref(false)
const error = ref('')
const exportParams = computed<ExportParams | null>(() => (result.value ? { type: 'cluster', date: result.value.date } : null))

async function run() {
  running.value = true
  error.value = ''
  try {
    result.value = await api.runCluster({ k: k.value, method: method.value })
  } catch (reason) {
    if (isAbort(reason)) return
    if (reason instanceof ApiError && reason.status === 503) {
      available.value = false
      missingMessage.value = reason.detail
    } else error.value = reason instanceof ApiError ? reason.detail : '聚类失败'
  } finally {
    running.value = false
  }
}

onMounted(async () => {
  try {
    const status = await api.clusterStatus()
    available.value = status.available
    missingMessage.value = status.message
  } catch {
    available.value = null
  }
  result.value = await api.cluster().catch(() => null)
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>聚类分析</h1>
      <p class="lede">把最近 3 个交易日涨停股的涨停前三日形态（K 线、成交量、量比）分组，看看有几类典型走法。不含涨停当天，不看未来。</p>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>组数</span>
        <select v-model="k">
          <option value="auto">自动</option>
          <option v-for="n in [2, 3, 4, 5, 6]" :key="n" :value="n">{{ n }}</option>
        </select>
      </label>
      <label class="field">
        <span>方法</span>
        <select v-model="method">
          <option value="kmeans">KMeans</option>
          <option value="agglomerative">层次聚类</option>
        </select>
      </label>
      <button class="btn" type="button" :disabled="running || available === false" @click="run">{{ running ? '计算中…' : '运行聚类' }}</button>
      <ExportButton :params="exportParams" />
    </div>
  </header>

  <section v-if="available === false" class="card">
    <h2>scikit-learn 未安装</h2>
    <p>{{ missingMessage || '聚类需要本地安装 scikit-learn' }}</p>
    <p class="muted">Windows 绿色版 EXE 不带聚类依赖。请用源码方式运行：在项目目录执行 <span class="code">.venv\Scripts\pip install -r requirements-cluster.txt</span>，或双击 <span class="code">run_cluster_windows.bat</span>。</p>
  </section>
  <p v-if="error" class="alert">{{ error }}</p>

  <template v-if="result">
    <section class="card">
      <div class="card-head">
        <h2>{{ result.date }} · {{ result.sample_count }} 个样本 · {{ result.k ?? 0 }} 组</h2>
        <span class="muted">
          {{ result.method === 'agglomerative' ? '层次聚类' : 'KMeans' }}
          <template v-if="result.silhouette != null"> · 轮廓系数 {{ result.silhouette }}</template>
          · 样本日 {{ result.pool_dates.join('、') }}
        </span>
      </div>
      <Disclaimer :text="result.disclaimer" />
      <p v-if="result.note" class="empty">{{ result.note }}</p>
      <ClusterScatter v-else :result="result" :colors="COLORS" />
    </section>
    <div class="monitor-grid" style="grid-template-columns: repeat(auto-fill, minmax(300px, 1fr))">
      <article v-for="cluster in result.clusters" :key="cluster.id" class="card">
        <div class="card-head">
          <h2 :style="{ color: COLORS[cluster.id % COLORS.length] }">组{{ cluster.id + 1 }} · {{ cluster.name }}</h2>
          <span class="muted">{{ cluster.size }} 只<template v-if="cluster.return_3d != null"> · 三日涨幅 {{ cluster.return_3d.toFixed(2) }}%</template></span>
        </div>
        <ClusterPattern v-if="cluster.pattern.length" :pattern="cluster.pattern" :color="COLORS[cluster.id % COLORS.length]" />
        <div class="chips" style="margin-top: 8px">
          <span v-for="member in cluster.members" :key="member.code + member.limit_date" class="chip" :title="`涨停日 ${member.limit_date}`">{{ member.code }} {{ member.name }}</span>
        </div>
      </article>
    </div>
  </template>
  <p v-else-if="available !== false" class="empty">还没有聚类结果，点「运行聚类」</p>
</template>
