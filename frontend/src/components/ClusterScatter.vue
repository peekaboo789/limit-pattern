<script setup lang="ts">
import { type EChartsCoreOption } from '@/charts/register'
import { CHART_FONT, axisLabel, splitLine, tooltipBase } from '@/charts/theme'
import { useChart } from '@/composables/useChart'
import type { ClusterGroupOut, ClusterPointOut } from '@/api/types'
import { watch } from 'vue'

const props = defineProps<{
  points: ClusterPointOut[]
  groups: ClusterGroupOut[]
  height?: number
}>()

/** 配色只用来区分组，不表达好坏：这一页没有「哪组更强」的读数。 */
const PALETTE = ['#e8b86d', '#5ee0d0', '#7eb0ff', '#ff8fa3', '#c3a6ff', '#9ad36a', '#f0956b']

const colorOf = (clusterId: number) => PALETTE[clusterId % PALETTE.length]

const samples = () => props.points.filter((point) => !point.is_centroid)
const centroids = () => props.points.filter((point) => point.is_centroid)

function option(): EChartsCoreOption {
  const nameOf = new Map(props.groups.map((group) => [group.cluster_id, group.name]))
  const ids = [...new Set(samples().map((point) => point.cluster_id))].sort((a, b) => a - b)
  const series: Record<string, unknown>[] = ids.map((clusterId) => ({
    name: nameOf.get(clusterId) ?? `组 ${clusterId}`,
    type: 'scatter' as const,
    symbolSize: 9,
    itemStyle: { color: colorOf(clusterId), opacity: 0.85 },
    data: samples()
      .filter((point) => point.cluster_id === clusterId)
      .map((point) => ({
        value: [point.x, point.y],
        code: point.code,
        stock: point.name,
        group: point.group,
      })),
  }))
  series.push({
    name: '组质心',
    type: 'scatter' as const,
    symbol: 'diamond',
    symbolSize: 20,
    z: 6,
    data: centroids().map((point) => ({
      value: [point.x, point.y],
      code: '',
      stock: '',
      group: point.group,
      itemStyle: {
        color: colorOf(point.cluster_id),
        borderColor: '#0b1018',
        borderWidth: 2,
      },
    })),
  })

  return {
    backgroundColor: 'transparent',
    animationDuration: 280,
    textStyle: { fontFamily: CHART_FONT },
    tooltip: {
      ...tooltipBase,
      trigger: 'item',
      formatter: (params: any) => {
        const data = params.data ?? {}
        const lines = [data.code ? `${data.code} ${data.stock ?? ''}` : (data.group ?? params.seriesName)]
        if (data.code && data.group) lines.push(data.group)
        lines.push(`坐标 ${params.value[0]}，${params.value[1]}`)
        return lines.join('<br/>')
      },
    },
    legend: {
      type: 'scroll',
      bottom: 0,
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: '#93a4bf', fontSize: 11, fontFamily: CHART_FONT },
    },
    grid: { left: 12, right: 20, top: 16, bottom: 46, containLabel: true },
    xAxis: {
      type: 'value',
      name: '第一主成分',
      nameLocation: 'middle',
      nameGap: 26,
      nameTextStyle: { color: '#93a4bf', fontSize: 11, fontFamily: CHART_FONT },
      axisLabel,
      splitLine,
      axisLine: { show: false },
    },
    yAxis: {
      type: 'value',
      name: '第二主成分',
      nameTextStyle: { color: '#93a4bf', fontSize: 11, fontFamily: CHART_FONT },
      axisLabel,
      splitLine,
      axisLine: { show: false },
    },
    series,
  }
}

const { el, render } = useChart(option)
watch(() => [props.points, props.groups], () => render(), { deep: true })
</script>

<template>
  <div
    ref="el"
    class="chart"
    role="img"
    aria-label="形态散点"
    :style="{ height: `${height ?? 400}px` }"
  />
</template>
