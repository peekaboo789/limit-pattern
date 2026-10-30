<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { ExportParams, RankingBacktest, SimilarityBacktest } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import { session } from '@/stores/session'
import { computed, onMounted, ref } from 'vue'

const sim = ref<SimilarityBacktest | null>(null)
const ai = ref<RankingBacktest | null>(null)
const top = ref(50)
const loading = ref(false)
const error = ref('')
const exportParams = computed<ExportParams | null>(() => {
  const date = session.health?.data_date
  return date ? { type: 'backtest', date, top: top.value } : null
})

function rate(value: number | null) {
  return value == null ? '—' : `${value.toFixed(1)}%`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [left, right] = await Promise.all([api.similarityBacktest(top.value), api.rankingBacktest()])
    sim.value = left
    ai.value = right
  } catch (reason) {
    if (!isAbort(reason)) error.value = reason instanceof ApiError ? reason.detail : '回测加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>回测</h1>
      <p class="lede">评估日只用当天及以前的行情打分，再看下一个交易日的实际表现，不看未来。明细只保留约一周，样本很少，结论仅供参考。</p>
    </div>
    <div class="toolbar">
      <select v-model.number="top" @change="load">
        <option :value="20">扫描前 20</option>
        <option :value="50">扫描前 50</option>
        <option :value="100">扫描前 100</option>
      </select>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">重新计算</button>
      <ExportButton :params="exportParams" />
    </div>
  </header>
  <p v-if="error" class="alert">{{ error }}</p>
  <div v-if="loading" class="skeleton" />
  <template v-else>
    <section class="card">
      <div class="card-head"><h2>相似度分档 · 下一交易日</h2></div>
      <Disclaimer :text="sim?.disclaimer" />
      <div class="table-wrap">
        <table>
          <thead><tr><th>分档</th><th class="right">样本</th><th class="right">涨停</th><th class="right">涨停率</th><th class="right">盘中冲 3% 率</th><th class="right">次日平均涨幅</th></tr></thead>
          <tbody>
            <tr v-for="band in sim?.bands ?? []" :key="band.band">
              <td>{{ band.band }}</td><td class="right">{{ band.count }}</td><td class="right">{{ band.limit_up }}</td>
              <td class="right">{{ rate(band.limit_up_rate) }}</td><td class="right">{{ rate(band.up3_rate) }}</td>
              <td class="right">{{ band.avg_next_pct == null ? '—' : `${band.avg_next_pct.toFixed(2)}%` }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!sim?.days.length" class="empty">至少需要两个交易日的行情才能回测</p>
    </section>
    <section class="card">
      <div class="card-head">
        <h2>AI 前 20 榜单 · 下一交易日</h2>
        <span v-if="ai" class="muted">合计 {{ ai.total.count }} 只次，涨停 {{ ai.total.limit_up }}（{{ rate(ai.total.limit_up_rate) }}）</span>
      </div>
      <Disclaimer :text="ai?.disclaimer" />
      <div v-if="!ai?.days.length" class="empty">还没有已经走完下一交易日的 AI 榜单</div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>榜单日</th><th>次日</th><th class="right">样本</th><th class="right">涨停</th><th class="right">涨停率</th><th class="right">冲 3% 率</th><th class="right">次日平均涨幅</th><th>涨停代码</th></tr></thead>
          <tbody>
            <tr v-for="row in ai.days" :key="row.date">
              <td>{{ row.date }}</td><td>{{ row.next_date }}</td><td class="right">{{ row.count }}</td><td class="right">{{ row.limit_up }}</td>
              <td class="right">{{ rate(row.limit_up_rate) }}</td><td class="right">{{ rate(row.up3_rate) }}</td>
              <td class="right">{{ row.avg_next_pct == null ? '—' : `${row.avg_next_pct.toFixed(2)}%` }}</td>
              <td class="wrap code">{{ (row.hits ?? []).join(', ') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </template>
</template>
