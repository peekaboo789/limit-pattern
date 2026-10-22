<script setup lang="ts">
import { CHART_FONT, axisLabel, axisLine, splitLine, tooltipBase } from '@/charts/theme'
import { useChart } from '@/composables/useChart'
import type { KBar } from '@/api/types'
import type { EChartsCoreOption } from '@/charts/register'
import { formatAmount, formatPct, formatPrice, formatVolume } from '@/utils/format'
import { watch } from 'vue'

const props = withDefaults(defineProps<{
  bars: KBar[]
  windowDates?: string[]
  height?: number
}>(), {
  windowDates: () => [],
  height: 360,
})

function indexOf(params: unknown): number {
  const first = Array.isArray(params) ? params[0] : params
  if (first && typeof first === 'object' && 'dataIndex' in first) {
    const value = (first as { dataIndex?: unknown }).dataIndex
    if (typeof value === 'number') return value
  }
  return 0
}

function option(): EChartsCoreOption {
  const bars = props.bars
  const dates = bars.map((bar) => bar.date)
  const present = new Set(dates)
  const windowDates = (props.windowDates ?? []).filter((date) => present.has(date))
  const start = bars.length <= 48 ? 0 : Math.round((1 - 48 / bars.length) * 100)
  const marks = bars
    .filter((bar) => {
      const nearLimit = Math.abs(bar.pct_chg - 10) < 0.4 || Math.abs(bar.pct_chg - 20) < 0.5
      return nearLimit && bar.high <= bar.close + 0.03
    })
    .map((bar) => ({
      coord: [bar.date, bar.high],
      symbol: 'circle',
      symbolSize: 7,
      itemStyle: { color: '#ff5d73' },
      label: { show: true, formatter: '涨停', color: '#ffd5dc', fontSize: 10, position: 'top' },
    }))
  return {
    backgroundColor: 'transparent',
    animationDuration: 280,
    textStyle: { fontFamily: CHART_FONT },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      ...tooltipBase,
      trigger: 'axis',
      formatter: (params: unknown) => {
        const bar = bars[indexOf(params)]
        if (!bar) return ''
        const color = bar.pct_chg >= 0 ? '#ff5d73' : '#2fdb9a'
        return [
          `<div style="color:#f3d7a1;margin-bottom:4px">${bar.date}</div>`,
          `开盘 ${formatPrice(bar.open)}`,
          `最高 ${formatPrice(bar.high)}`,
          `最低 ${formatPrice(bar.low)}`,
          `收盘 ${formatPrice(bar.close)}`,
          `<span style="color:${color}">涨跌 ${formatPct(bar.pct_chg)}</span>`,
          `成交量 ${formatVolume(bar.volume)}`,
          `成交额 ${formatAmount(bar.amount)}`,
        ].join('<br/>')
      },
    },
    grid: [
      { left: 58, right: 16, top: 28, height: '58%' },
      { left: 58, right: 16, top: '78%', height: '12%' },
    ],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine, axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, boundaryGap: true, axisLine, axisLabel, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, axisLine: { show: false }, axisLabel, splitLine },
      { scale: true, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start, end: 100 },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        start,
        end: 100,
        height: 16,
        bottom: 4,
        borderColor: 'transparent',
        backgroundColor: 'rgba(255,255,255,0.03)',
        fillerColor: 'rgba(232,184,109,0.18)',
        handleStyle: { color: '#e8b86d' },
        textStyle: { color: '#93a4bf' },
        moveHandleSize: 0,
      },
    ],
    series: [
      {
        type: 'candlestick',
        name: 'K线',
        data: bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]),
        itemStyle: {
          color: '#ff5d73',
          color0: '#1aa875',
          borderColor: '#ff8b9b',
          borderColor0: '#2fdb9a',
        },
        markArea: windowDates.length
          ? {
              itemStyle: { color: 'rgba(232, 184, 109, 0.2)' },
              label: { color: '#f6e2bc', fontSize: 11, fontFamily: CHART_FONT },
              data: [[
                { name: '对比窗口', xAxis: windowDates[0], label: { position: 'insideTop' } },
                { xAxis: windowDates[windowDates.length - 1] },
              ]],
            }
          : undefined,
        markPoint: marks.length ? { data: marks } : undefined,
      },
      {
        type: 'bar',
        name: '成交量',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: bars.map((bar) => ({
          value: bar.volume,
          itemStyle: { color: bar.close >= bar.open ? 'rgba(255,93,115,0.75)' : 'rgba(47,219,154,0.7)' },
        })),
        barMaxWidth: 10,
      },
    ],
  }
}

const { el, render } = useChart(option)
watch(() => [props.bars, props.windowDates], () => render(), { deep: true })
</script>

<template>
  <div ref="el" class="chart" role="img" aria-label="日K线" :style="{ height: `${height}px` }" />
</template>
