<script setup lang="ts">
import type { EChartsCoreOption } from '@/charts/register'
import { CHART_FONT, axisLabel, splitLine, tooltipBase } from '@/charts/theme'
import { useChart } from '@/composables/useChart'
import type { ClusterPatternRow } from '@/api/types'
import { watch } from 'vue'

const props = defineProps<{ pattern: ClusterPatternRow[]; color: string }>()

function option(): EChartsCoreOption {
  const labels = props.pattern.map((row) => ({ t3: '前3日', t2: '前2日', t1: '前1日' })[row.day] ?? row.day)
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: CHART_FONT },
    tooltip: { ...tooltipBase, trigger: 'axis' },
    grid: { left: 4, right: 4, top: 8, bottom: 4, containLabel: true },
    xAxis: { type: 'category', data: labels, axisLabel },
    yAxis: [
      { type: 'value', axisLabel, splitLine },
      { type: 'value', axisLabel, splitLine: { show: false } },
    ],
    series: [
      { type: 'bar', name: '收盘涨跌%', data: props.pattern.map((row) => row.close_change), itemStyle: { color: props.color } },
      { type: 'line', name: '5日量比', yAxisIndex: 1, data: props.pattern.map((row) => row.volume_ratio_5), lineStyle: { color: '#e8b86d' }, itemStyle: { color: '#e8b86d' } },
    ],
  }
}

const { el, render } = useChart(option)
watch(() => props.pattern, render)
</script>

<template>
  <div ref="el" style="height: 120px; width: 100%" />
</template>
