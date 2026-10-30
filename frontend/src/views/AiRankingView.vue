<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { AiRanking, ExportParams } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import { computed, onMounted, ref } from 'vue'

const dates = ref<string[]>([])
const selected = ref('')
const ranking = ref<AiRanking | null>(null)
const loading = ref(false)
const generating = ref(false)
const error = ref('')

const exportParams = computed<ExportParams | null>(() => (ranking.value ? { type: 'ai_ranking', date: ranking.value.date } : null))

async function loadDates() {
  try {
    dates.value = (await api.aiRankingDates()).dates
  } catch (reason) {
    if (!isAbort(reason)) dates.value = []
  }
}

async function open(date?: string) {
  loading.value = true
  error.value = ''
  try {
    ranking.value = await api.aiRanking(date)
    selected.value = ranking.value.date
  } catch (reason) {
    ranking.value = null
    if (!isAbort(reason)) error.value = reason instanceof ApiError ? reason.detail : 'AI 榜单加载失败'
  } finally {
    loading.value = false
  }
}

async function generate() {
  generating.value = true
  error.value = ''
  try {
    ranking.value = await api.generateAiRanking()
    selected.value = ranking.value.date
    await loadDates()
  } catch (reason) {
    if (isAbort(reason)) return
    if (reason instanceof ApiError && reason.status === 409) error.value = '还没配置 AI 接口，请先到「设置」里填写'
    else error.value = reason instanceof ApiError ? reason.detail : '生成失败'
  } finally {
    generating.value = false
  }
}

onMounted(async () => {
  await loadDates()
  await open()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>AI 榜单</h1>
      <p class="lede">从当前最相似的前 100 只里，由 AI 按形态和量能打分，留前 20 名。交易日 14:50、15:10 自动生成。</p>
    </div>
    <div class="toolbar">
      <select v-if="dates.length" v-model="selected" @change="open(selected)">
        <option v-for="day in dates" :key="day" :value="day">{{ day }}</option>
      </select>
      <button class="btn" type="button" :disabled="generating" @click="generate">{{ generating ? 'AI 打分中…' : '立即生成' }}</button>
      <ExportButton :params="exportParams" />
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <section class="card">
    <div class="card-head">
      <h2>{{ ranking ? `${ranking.date} 前 20 名` : '前 20 名' }}</h2>
      <span v-if="ranking" class="muted">
        候选 {{ ranking.candidate_count }} 只 · 样本池 {{ ranking.pool_size }} 条 · 生成于 {{ ranking.run_at.replace('T', ' ').slice(0, 19) }}
        <template v-if="ranking.model"> · {{ ranking.model }}</template>
      </span>
    </div>
    <Disclaimer :text="ranking?.disclaimer" />
    <div v-if="loading" class="skeleton" />
    <div v-else-if="!ranking?.items.length" class="empty">还没有 AI 榜单，点「立即生成」试试</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th><th>代码</th><th>名称</th><th>板块</th>
            <th class="right">AI 评分</th><th class="right">相似度</th><th class="right">形态</th>
            <th class="right">成交量</th><th class="right">量比</th><th>最相似涨停</th><th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ranking.items" :key="row.code">
            <td class="rank" :class="{ top: row.rank <= 3 }">{{ String(row.rank).padStart(2, '0') }}</td>
            <td class="code">{{ row.code }}</td>
            <td>{{ row.name }}</td>
            <td class="muted">{{ row.board_name }}</td>
            <td class="right"><b>{{ row.ai_score.toFixed(1) }}</b></td>
            <td class="right">{{ row.similarity.toFixed(1) }}</td>
            <td class="right">{{ row.shape_score.toFixed(1) }}</td>
            <td class="right">{{ row.volume_score.toFixed(1) }}</td>
            <td class="right">{{ row.volume_pct_score.toFixed(1) }}</td>
            <td class="wrap">{{ row.best_match.name }} <span class="muted code">{{ row.best_match.code }} · {{ row.best_match.limit_date }}</span></td>
            <td class="wrap">{{ row.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
