<script setup lang="ts">
import type { EChartsCoreOption } from '@/charts/register'
import { CHART_FONT, axisLabel, splitLine, tooltipBase } from '@/charts/theme'
import { useChart } from '@/composables/useChart'
import type { ClusterResult } from '@/api/types'
import { watch } from 'vue'

const props = defineProps<{ result: ClusterResult; colors: string[] }>()

function option(): EChartsCoreOption {
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: CHART_FONT },
    tooltip: { ...tooltipBase, trigger: 'item', formatter: (p: { data: [number, number, string] }) => p.data[2] },
    legend: { textStyle: { color: '#9aa7b8' }, top: 0 },
    grid: { left: 8, right: 16, top: 32, bottom: 8, containLabel: true },
    xAxis: { type: 'value', name: 'PC1', axisLabel, splitLine, scale: true },
    yAxis: { type: 'value', name: 'PC2', axisLabel, splitLine, scale: true },
    series: props.result.clusters.map((cluster) => ({
      type: 'scatter',
      name: `组${cluster.id + 1} ${cluster.name}`,
      symbolSize: 10,
      itemStyle: { color: props.colors[cluster.id % props.colors.length] },
      data: props.result.points
        .filter((point) => point.cluster === cluster.id)
        .map((point) => [point.x, point.y, `${point.code} ${point.name}`]),
    })),
  }
}

const { el, render } = useChart(option)
watch(() => props.result, render)
</script>

<template>
  <div ref="el" style="height: 360px; width: 100%" />
</template>
