<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { ExportParams, NextDayPool } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import { computed, onMounted, ref } from 'vue'

const pool = ref<NextDayPool | null>(null)
const top = ref(30)
const loading = ref(false)
const error = ref('')
const exportParams = computed<ExportParams | null>(() => (pool.value?.date ? { type: 'next_day_pool', date: pool.value.date, top: top.value } : null))

function rate(value: number | null) {
  return value == null ? '—' : `${value.toFixed(1)}%`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    pool.value = await api.nextDayPool(top.value)
  } catch (reason) {
    if (!isAbort(reason)) error.value = reason instanceof ApiError ? reason.detail : '观察池加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>次日观察池</h1>
      <p class="lede">按最新交易日的形态相似度挑出前 N 只，并附上同一分档最近几天的历史发生率。只是观察名单，不是预测。</p>
    </div>
    <div class="toolbar">
      <select v-model.number="top" @change="load">
        <option :value="20">20</option>
        <option :value="30">30</option>
        <option :value="50">50</option>
      </select>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">刷新</button>
      <ExportButton :params="exportParams" />
    </div>
  </header>
  <p v-if="error" class="alert">{{ error }}</p>
  <section class="card">
    <div class="card-head"><h2>{{ pool?.date ?? '—' }} 观察池</h2></div>
    <Disclaimer :text="pool?.disclaimer" />
    <div v-if="loading" class="skeleton" />
    <div v-else-if="!pool?.items.length" class="empty">没有数据</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr><th>排名</th><th>代码</th><th>名称</th><th class="right">相似度</th><th>分档</th><th class="right">分档样本</th><th class="right">分档历史涨停率</th><th class="right">分档历史冲 3% 率</th><th>最相似涨停</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in pool.items" :key="row.code">
            <td class="rank">{{ String(index + 1).padStart(2, '0') }}</td>
            <td class="code">{{ row.code }}</td>
            <td>{{ row.name }}</td>
            <td class="right"><b>{{ row.score.toFixed(1) }}</b></td>
            <td>{{ row.band }}</td>
            <td class="right">{{ row.band_samples }}</td>
            <td class="right">{{ rate(row.band_limit_up_rate) }}</td>
            <td class="right">{{ rate(row.band_up3_rate) }}</td>
            <td class="wrap">{{ row.best_match.name }} <span class="muted code">{{ row.best_match.code }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
