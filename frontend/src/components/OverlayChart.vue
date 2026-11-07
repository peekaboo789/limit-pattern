<script setup lang="ts">
import type { EChartsCoreOption } from '@/charts/register'
import { CHART_FONT, axisLabel, axisLine, splitLine, tooltipBase } from '@/charts/theme'
import { useChart } from '@/composables/useChart'
import type { KBar } from '@/api/types'
import { formatPct, formatPrice, shortDate } from '@/utils/format'
import { computed, watch } from 'vue'

const props = defineProps<{
  targetName: string
  matchName: string
  targetWindow: string[]
  matchWindow: string[]
  targetBars: KBar[]
  matchBars: KBar[]
  height?: number
}>()

interface Slot {
  label: string
  targetDate: string
  matchDate: string
  target: KBar | null
  match: KBar | null
}

const slots = computed<Slot[]>(() => {
  const count = Math.max(props.targetWindow.length, props.matchWindow.length)
  const targetMap = new Map(props.targetBars.map((bar) => [bar.date, bar]))
  const matchMap = new Map(props.matchBars.map((bar) => [bar.date, bar]))
  return Array.from({ length: count }, (_, index) => {
    const targetDate = props.targetWindow[index] ?? ''
    const matchDate = props.matchWindow[index] ?? ''
    return {
      label: `D${index + 1}`,
      targetDate,
      matchDate,
      target: targetDate ? targetMap.get(targetDate) ?? null : null,
      match: matchDate ? matchMap.get(matchDate) ?? null : null,
    }
  })
})

function baseOf(bars: Array<KBar | null>, field: 'open' | 'volume'): number {
  const found = bars.find((bar) => bar && bar[field] > 0)
  return found ? found[field] : 1
}

function candle(bar: KBar | null, base: number): number[] | string[] {
  if (!bar) return ['-', '-', '-', '-']
  const scale = (price: number) => Math.round((price / base - 1) * 10000) / 100
  return [scale(bar.open), scale(bar.close), scale(bar.low), scale(bar.high)]
}

function option(): EChartsCoreOption {
  const rows = slots.value
  const targetBase = baseOf(rows.map((row) => row.target), 'open')
  const matchBase = baseOf(rows.map((row) => row.match), 'open')
  const targetVol = baseOf(rows.map((row) => row.target), 'volume')
  const matchVol = baseOf(rows.map((row) => row.match), 'volume')
  const labels = rows.map((row) => row.label)
  return {
    backgroundColor: 'transparent',
    animationDuration: 280,
    textStyle: { fontFamily: CHART_FONT },
    legend: {
      data: [props.targetName, props.matchName],
      textStyle: { color: '#d5deec', fontFamily: CHART_FONT },
      top: 0,
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      ...tooltipBase,
      trigger: 'axis',
      formatter: (params: unknown) => {
        const first = Array.isArray(params) ? params[0] : params
        const index = first && typeof first === 'object' && 'dataIndex' in first
          ? Number((first as { dataIndex?: number }).dataIndex ?? 0)
          : 0
        const row = rows[index]
        if (!row) return ''
        const line = (name: string, date: string, bar: KBar | null, color: string) => {
          if (!bar) return `<div style="color:${color}">${name} · 无行情</div>`
          return `<div style="margin-top:6px;color:${color}">${name} · ${date}<br/>开 ${formatPrice(bar.open)} 收 ${formatPrice(bar.close)} 高 ${formatPrice(bar.high)} 低 ${formatPrice(bar.low)}<br/>涨跌 ${formatPct(bar.pct_chg)}</div>`
        }
        return [
          `<div style="font-weight:700">窗口 ${row.label}</div>`,
          line(props.targetName, row.targetDate, row.target, '#e8b86d'),
          line(props.matchName, row.matchDate, row.match, '#5ee0d0'),
        ].join('')
      },
    },
    grid: [
      { left: 52, right: 12, top: 36, height: '58%' },
      { left: 52, right: 12, top: '78%', height: '14%' },
    ],
    xAxis: [
      { type: 'category', data: labels, axisLine, axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', data: labels, gridIndex: 1, axisLine, axisLabel, splitLine: { show: false } },
    ],
    yAxis: [
      {
        scale: true,
        axisLine: { show: false },
        axisLabel: { ...axisLabel, formatter: '{value}%' },
        splitLine,
      },
      { scale: true, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    series: [
      {
        name: props.targetName,
        type: 'candlestick',
        data: rows.map((row) => candle(row.target, targetBase)),
        barWidth: 18,
        itemStyle: { color: '#e8b86d', color0: '#6a5128', borderColor: '#ffe1b0', borderColor0: '#e8b86d' },
        markLine: {
          symbol: 'none',
          silent: true,
          lineStyle: { color: 'rgba(255,255,255,0.18)', type: 'dashed' },
          data: [{ yAxis: 0 }],
          label: { show: false },
        },
      },
      {
        name: props.matchName,
        type: 'candlestick',
        data: rows.map((row) => candle(row.match, matchBase)),
        barWidth: 18,
        itemStyle: { color: '#5ee0d0', color0: '#1c6860', borderColor: '#c8fff6', borderColor0: '#5ee0d0' },
      },
      {
        name: `${props.targetName}量`,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => (row.target ? Math.round((row.target.volume / targetVol) * 1000) / 10 : '-')),
        itemStyle: { color: 'rgba(232,184,109,0.55)' },
        barMaxWidth: 14,
      },
      {
        name: `${props.matchName}量`,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => (row.match ? Math.round((row.match.volume / matchVol) * 1000) / 10 : '-')),
        itemStyle: { color: 'rgba(94,224,208,0.45)' },
        barMaxWidth: 14,
      },
    ],
  }
}

const { el, render } = useChart(option)
watch(
  () => [props.targetBars, props.matchBars, props.targetWindow, props.matchWindow, props.targetName, props.matchName],
  () => render(),
  { deep: true },
)

const captionTarget = computed(() => props.targetWindow.map(shortDate).join(' / '))
const captionMatch = computed(() => props.matchWindow.map(shortDate).join(' / '))
</script>

<template>
  <div>
    <div class="window-legend">
      <span><i class="swatch gold" />{{ targetName }} {{ captionTarget }}</span>
      <span><i class="swatch cyan" />{{ matchName }} {{ captionMatch }}</span>
    </div>
    <div ref="el" class="chart" role="img" aria-label="K线叠加对比" :style="{ height: `${height ?? 320}px` }" />
  </div>
</template>
