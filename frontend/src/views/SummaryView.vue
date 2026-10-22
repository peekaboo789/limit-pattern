<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { SummaryListItem } from '@/api/types'
import SectorChart from '@/components/SectorChart.vue'
import { formatDateTime } from '@/utils/format'
import { normalizeSummary, type NormalizedSummary } from '@/utils/summary'
import { computed, onMounted, ref } from 'vue'

const list = ref<SummaryListItem[]>([])
const selected = ref('')
const raw = ref<unknown>(null)
const showRaw = ref(false)
const loadingList = ref(false)
const loadingDetail = ref(false)
const generating = ref(false)
const error = ref('')

const view = computed<NormalizedSummary | null>(() => (raw.value ? normalizeSummary(raw.value) : null))
const ladderMax = computed(() => Math.max(1, ...(view.value?.ladder.map((row) => row.count) ?? [1])))
const headline = computed(() => view.value?.headline || list.value.find((item) => item.date === selected.value)?.headline || '')

async function loadList(prefer?: string) {
  loadingList.value = true
  error.value = ''
  try {
    const response = await api.summaries()
    list.value = response.items
    const next = prefer && response.items.some((item) => item.date === prefer)
      ? prefer
      : response.items[0]?.date ?? ''
    if (next) await open(next)
  } catch (reason) {
    if (isAbort(reason)) return
    error.value = reason instanceof ApiError ? reason.detail : '总结列表加载失败'
  } finally {
    loadingList.value = false
  }
}

async function open(date: string) {
  selected.value = date
  showRaw.value = false
  loadingDetail.value = true
  error.value = ''
  try {
    raw.value = await api.summary(date)
  } catch (reason) {
    if (isAbort(reason)) return
    raw.value = null
    error.value = reason instanceof ApiError ? reason.detail : '总结加载失败'
  } finally {
    loadingDetail.value = false
  }
}

async function generate() {
  if (!selected.value) return
  generating.value = true
  error.value = ''
  try {
    raw.value = await api.generateSummary(selected.value)
    const response = await api.summaries()
    list.value = response.items
  } catch (reason) {
    if (!isAbort(reason)) error.value = reason instanceof ApiError ? reason.detail : '重新生成失败'
  } finally {
    generating.value = false
  }
}

onMounted(() => {
  void loadList()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>每日总结</h1>
      <p class="lede">把当天涨停结构整理成板块、连板梯队和要点。这是复盘，不是交易建议。</p>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <div v-if="loadingList && !list.length" class="skeleton" />

  <div v-else class="sum-layout">
    <section class="card">
      <div class="card-head"><h2>日期</h2></div>
      <div class="day-list">
        <button
          v-for="item in list"
          :key="item.date"
          class="day-btn"
          :class="{ active: item.date === selected }"
          type="button"
          @click="open(item.date)"
        >
          <strong>{{ item.date }}</strong>
          <span class="muted">涨停 {{ item.limitup_count }} 只</span>
          <span class="clamp">{{ item.headline }}</span>
        </button>
      </div>
      <p v-if="!list.length" class="empty">还没有每日总结</p>
    </section>

    <div class="stack">
      <div v-if="loadingDetail" class="skeleton" />
      <template v-else-if="view">
        <section class="card">
          <div class="card-head">
            <div>
              <h2>{{ view.date }} · {{ headline }}</h2>
              <p class="muted" style="margin: 4px 0 0">
                涨停 {{ view.limitupCount ?? '—' }} 只
                <template v-if="view.model"> · {{ view.model }}</template>
                <template v-if="view.generatedAt"> · {{ formatDateTime(view.generatedAt) }}</template>
              </p>
            </div>
            <div class="toolbar">
              <button class="btn btn-ghost" type="button" @click="showRaw = !showRaw">{{ showRaw ? '隐藏原始 JSON' : '查看原始 JSON' }}</button>
              <button class="btn" type="button" :disabled="generating" @click="generate">{{ generating ? '生成中…' : '重新生成' }}</button>
            </div>
          </div>
          <pre v-if="showRaw" class="raw">{{ JSON.stringify(raw, null, 2) }}</pre>
        </section>

        <section class="card">
          <div class="card-head"><h3>要点</h3></div>
          <ol v-if="view.keyPoints.length" class="points">
            <li v-for="(point, index) in view.keyPoints" :key="index">{{ point }}</li>
          </ol>
          <p v-else class="empty">没有要点</p>
        </section>

        <div class="two">
          <section class="card">
            <div class="card-head"><h3>板块</h3></div>
            <SectorChart v-if="view.sectors.some((item) => item.count > 0)" :sectors="view.sectors" />
            <div v-else class="empty">没有板块统计</div>
          </section>
          <section class="card">
            <div class="card-head"><h3>连板梯队</h3></div>
            <div v-if="!view.ladder.length" class="empty">没有梯队</div>
            <div v-for="row in view.ladder" :key="row.label" class="ladder-row">
              <span>{{ row.label }}</span>
              <span class="ladder-track"><i class="ladder-fill" :style="{ width: `${(row.count / ladderMax) * 100}%` }" /></span>
              <b>{{ row.count }}</b>
              <span class="muted">{{ row.examples.join('、') }}</span>
            </div>
          </section>
        </div>

        <section class="card">
          <div class="card-head"><h3>涨停形态</h3></div>
          <div v-if="!view.patterns.length" class="empty">没有形态统计</div>
          <div class="chips">
            <span v-for="pattern in view.patterns" :key="pattern.name" class="chip">
              {{ pattern.name }}<template v-if="pattern.count != null"> {{ pattern.count }}</template>
              <template v-if="pattern.note"> · {{ pattern.note }}</template>
            </span>
          </div>
        </section>

        <section v-if="view.volumes.length" class="feature-grid">
          <article v-for="feature in view.volumes" :key="feature.key" class="feature">
            <span class="muted">{{ feature.label }}</span>
            <b>{{ feature.display }}</b>
          </article>
        </section>
      </template>
    </div>
  </div>
</template>
