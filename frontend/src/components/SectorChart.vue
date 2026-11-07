<script setup lang="ts">
import { graphic, type EChartsCoreOption } from '@/charts/register'
import { CHART_FONT, axisLabel, splitLine, tooltipBase } from '@/charts/theme'
import { useChart } from '@/composables/useChart'
import type { SectorCard } from '@/utils/summary'
import { watch } from 'vue'

const props = defineProps<{
  sectors: SectorCard[]
  height?: number
}>()

function option(): EChartsCoreOption {
  const rows = [...props.sectors].reverse()
  return {
    backgroundColor: 'transparent',
    animationDuration: 280,
    textStyle: { fontFamily: CHART_FONT },
    tooltip: { ...tooltipBase, trigger: 'axis' },
    grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: 'value', axisLabel, splitLine, axisLine: { show: false } },
    yAxis: {
      type: 'category',
      data: rows.map((row) => (row.pct == null ? row.name : `${row.name} ${row.pct.toFixed(0)}%`)),
      axisLabel,
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar',
        data: rows.map((row) => row.count),
        barWidth: 12,
        itemStyle: {
          borderRadius: [0, 6, 6, 0],
          color: new graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#8d6a32' },
            { offset: 1, color: '#e8b86d' },
          ]),
        },
        label: { show: true, position: 'right', color: '#d5deec', fontFamily: CHART_FONT },
      },
    ],
  }
}

const { el, render } = useChart(option)
watch(() => props.sectors, () => render(), { deep: true })
</script>

<template>
  <div ref="el" class="chart" role="img" aria-label="板块分布" :style="{ height: `${height ?? 260}px` }" />
</template>
