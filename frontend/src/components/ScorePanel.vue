<script setup lang="ts">
import { scoreLabel } from '@/utils/labels'
import { clampPercent, formatScore, scoreColor } from '@/utils/format'
import { computed } from 'vue'

const props = defineProps<{
  score: number
  euclidean?: number | null
  cosine?: number | null
  groups?: Record<string, number>
}>()

const radius = 52
const circumference = 2 * Math.PI * radius

const metrics = computed(() => {
  const rows: { key: string; label: string; value: number }[] = []
  if (typeof props.euclidean === 'number') rows.push({ key: 'euclidean', label: '欧氏分 ×0.8', value: props.euclidean })
  if (typeof props.cosine === 'number') rows.push({ key: 'cosine', label: '余弦分 ×0.2', value: props.cosine })
  for (const [key, value] of Object.entries(props.groups ?? {})) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      rows.push({ key, label: scoreLabel(key), value })
    }
  }
  return rows
})

const offset = computed(() => circumference * (1 - clampPercent(props.score) / 100))
const color = computed(() => scoreColor(props.score))
</script>

<template>
  <div class="score-layout">
    <svg class="ring" viewBox="0 0 120 120" role="img" aria-label="综合相似度">
      <circle class="ring-track" cx="60" cy="60" :r="radius" />
      <circle
        class="ring-value"
        cx="60"
        cy="60"
        :r="radius"
        :stroke="color"
        :stroke-dasharray="circumference"
        :stroke-dashoffset="offset"
        transform="rotate(-90 60 60)"
      />
      <text class="ring-score" x="60" y="58" text-anchor="middle">{{ formatScore(score) }}</text>
      <text class="ring-label" x="60" y="76" text-anchor="middle">综合</text>
    </svg>
    <div>
      <div v-for="metric in metrics" :key="metric.key" class="metric">
        <span class="metric-name">{{ metric.label }}</span>
        <span class="metric-track">
          <i class="metric-fill" :style="{ width: `${clampPercent(metric.value)}%`, background: scoreColor(metric.value) }" />
        </span>
        <span class="metric-val">{{ formatScore(metric.value) }}</span>
      </div>
    </div>
  </div>
</template>
