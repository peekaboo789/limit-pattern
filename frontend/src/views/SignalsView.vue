<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { SignalReport, SignalTypeReport } from '@/api/types'
import type { EChartsCoreOption } from '@/charts/register'
import { CHART_FONT, axisLabel, axisLine, splitLine, tooltipBase } from '@/charts/theme'
import Disclaimer from '@/components/Disclaimer.vue'
import { useChart } from '@/composables/useChart'
import { computed, onMounted, ref, watch } from 'vue'

const report = ref<SignalReport | null>(null)
const types = ref<string[]>([])
const shadow = ref<Record<string, string>>({})
const selected = ref<string>('')
const error = ref('')
const loading = ref(false)
const generating = ref(false)

const HOLD_KEYS = ['1', '2', '3', '4', '5']

const byType = computed<Record<string, SignalTypeReport>>(() => report.value?.by_type ?? {})

const activeType = computed<SignalTypeReport | null>(() => {
  if (!report.value) return null
  if (selected.value && byType.value[selected.value]) return byType.value[selected.value]
  const entries = Object.values(byType.value)
  return entries[0] ?? null
})

const reliability = computed(() => {
  const cal = activeType.value?.calibration
  if (!cal) return null
  return cal
})

const calChart = useChart(() => {
  const cal = reliability.value
  const bins = cal?.reliability?.bins ?? []
  const labels = bins.map((b) => `${(b.lower * 100).toFixed(0)}%`)
  const confidence = bins.map((b) => (b.confidence == null ? null : +(b.confidence * 100).toFixed(1)))
  const hit = bins.map((b) => (b.hit_rate == null ? null : +(b.hit_rate * 100).toFixed(1)))
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: CHART_FONT },
    tooltip: { ...tooltipBase, trigger: 'axis' },
    legend: { data: ['平均置信度', '实际命中率'], textStyle: { color: '#d5deec', fontFamily: CHART_FONT }, top: 0 },
    grid: { left: 44, right: 16, top: 40, bottom: 28 },
    xAxis: { type: 'category', data: labels, axisLine, axisLabel, splitLine: { show: false } },
    yAxis: { type: 'value', max: 100, axisLine: { show: false }, axisLabel: { ...axisLabel, formatter: '{value}%' }, splitLine },
    series: [
      { name: '平均置信度', type: 'bar', data: confidence, barMaxWidth: 22, itemStyle: { color: 'rgba(94,224,208,0.7)' } },
      { name: '实际命中率', type: 'line', data: hit, symbol: 'circle', symbolSize: 7, lineStyle: { color: '#e8b86d' }, itemStyle: { color: '#e8b86d' } },
    ],
  } as EChartsCoreOption
})

watch(reliability, () => calChart.render())

function fmt(value: number | null | undefined, digits = 2, suffix = '%') {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value.toFixed(digits)}${suffix}`
}

function pct(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

const statusIcon: Record<string, string> = { live: '🟢', passed: '🟡', shadow: '⚪', degraded: '🟠', disabled: '🔴' }
const statusLabel: Record<string, string> = { live: '已上线', passed: '已达标', shadow: '影子期', degraded: '已降权', disabled: '已停用' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [rep, typeList, shadowList] = await Promise.all([api.signalReport(), api.signalTypes(), api.signalShadow()])
    report.value = rep
    types.value = typeList.items.map((t) => t.signal_type)
    shadow.value = shadowList.items
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '信号报告加载失败'
  } finally {
    loading.value = false
  }
}

async function generate() {
  generating.value = true
  error.value = ''
  try {
    const res = await api.generateSignals()
    const total = Object.values(res.counts).reduce((a, b) => a + b, 0)
    error.value = ''
    window.alert(`已生成 ${res.date} 的规则信号：${total} 条\n${Object.entries(res.counts).map(([k, v]) => `${k} ${v} 条`).join('，') || '无'}`)
    await load()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '信号生成失败'
  } finally {
    generating.value = false
  }
}

onMounted(load)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>信号有效性</h1>
      <p class="lede">每类信号发出那一刻的上下文落成不可变事件，事后在快照上重放验证。超额收益 = 个股收益 − 同期全市场等权中位数收益，是历史统计，不是预测。</p>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>信号类型</span>
        <select v-model="selected">
          <option value="">全部</option>
          <option v-for="t in types" :key="t" :value="t">{{ t }}</option>
        </select>
      </label>
      <button class="btn" type="button" :disabled="generating" @click="generate">{{ generating ? '生成中…' : '生成今日规则信号' }}</button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <Disclaimer :text="report?.disclaimer" />

  <div v-if="loading" class="skeleton" />
  <template v-else-if="report">
    <p v-if="report.empty" class="empty">{{ report.disclaimer }}</p>
    <template v-else>
      <section class="card">
        <div class="card-head">
          <h2>{{ activeType?.signal_type ?? '全部' }} · {{ activeType?.n_events ?? report.n_events }} 个事件</h2>
          <span v-if="shadow[activeType?.signal_type ?? '']" class="muted">
            {{ statusIcon[shadow[activeType?.signal_type ?? '']] ?? '⚪' }} {{ statusLabel[shadow[activeType?.signal_type ?? '']] ?? shadow[activeType?.signal_type ?? ''] }}
          </span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>持有期</th>
                <th class="right">样本</th>
                <th class="right">超额均值</th>
                <th class="right">超额中位</th>
                <th class="right">胜率</th>
                <th class="right">盈亏比</th>
                <th class="right">t 值</th>
                <th class="right">置换 p</th>
                <th class="right">BH</th>
                <th class="right">Holm</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="hold in HOLD_KEYS" :key="hold">
                <td>T+{{ hold }}</td>
                <td class="right">{{ activeType?.holds?.[hold]?.n ?? '—' }}</td>
                <td class="right">{{ fmt(activeType?.holds?.[hold]?.excess_mean) }}</td>
                <td class="right">{{ fmt(activeType?.holds?.[hold]?.excess_median) }}</td>
                <td class="right">{{ pct(activeType?.holds?.[hold]?.win_rate) }}</td>
                <td class="right">{{ fmt(activeType?.holds?.[hold]?.profit_loss_ratio) }}</td>
                <td class="right">{{ fmt(activeType?.holds?.[hold]?.t_stat) }}</td>
                <td class="right">{{ activeType?.holds?.[hold]?.p_value ?? '—' }}</td>
                <td class="right">{{ activeType?.holds?.[hold]?.significant_bh ? '✅ 显著' : '不显著' }}</td>
                <td class="right">{{ activeType?.holds?.[hold]?.significant_holm ? '✅ 显著' : '不显著' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint">显著性用置换检验（1000 次），p 值再经 BH（FDR）与 Holm（FWER）校正。样本不足时显示 —。</p>
      </section>

      <section v-if="reliability" class="card">
        <div class="card-head">
          <h2>AI 置信度校准</h2>
          <span class="muted">Brier {{ fmt(reliability.brier, 4, '') }} · ECE {{ fmt(reliability.ece, 4, '') }} · 平均置信 {{ pct(reliability.mean_confidence) }} · 实际命中 {{ pct(reliability.hit_rate) }}</span>
        </div>
        <p class="hint">若「平均置信度」持续高于「实际命中率」，说明 AI 自评偏乐观，应自动降权。置信度不是概率。</p>
        <div ref="calChart.el" class="chart" role="img" aria-label="可靠性曲线" :style="{ height: '280px' }" />
      </section>

      <section v-if="activeType?.market_states" class="card">
        <div class="card-head"><h2>分市场状态 · T+1 超额收益</h2></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>市场状态</th><th class="right">样本</th><th class="right">超额均值</th><th class="right">中位数</th><th class="right">胜率</th><th class="right">盈亏比</th></tr></thead>
            <tbody>
              <tr v-for="(stat, state) in activeType.market_states" :key="state">
                <td>{{ state === 'hot' ? '🔥 强势' : state === 'cold' ? '❄️ 弱势' : state === 'neutral' ? '➖ 中性' : state }}</td>
                <td class="right">{{ stat.n }}</td>
                <td class="right">{{ fmt(stat.mean) }}</td>
                <td class="right">{{ fmt(stat.median) }}</td>
                <td class="right">{{ pct(stat.win_rate) }}</td>
                <td class="right">{{ fmt(stat.profit_loss_ratio) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint">强势 = 涨停 ≥ 50 且炸板率 ≤ 40%；弱势 = 涨停 &lt; 20；其余中性。</p>
      </section>

      <section class="card">
        <div class="card-head">
          <h2>信号事件（只读）</h2>
          <span class="muted">共 {{ report.events.length }} 条</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>信号日</th><th>类型</th><th>代码</th><th>名称</th><th class="right">把握</th><th>T+1 净收益</th><th>T+1 超额</th></tr></thead>
            <tbody>
              <tr v-for="e in report.events.slice().reverse().slice(0, 50)" :key="e.signal_id">
                <td>{{ e.signal_date }}</td>
                <td>{{ e.signal_type }}</td>
                <td class="code">{{ e.code }}</td>
                <td>{{ e.name }}</td>
                <td class="right">{{ e.confidence == null ? '—' : e.confidence.toFixed(0) }}</td>
                <td class="right">{{ fmt(e.net_return_pct_1 as never) }}</td>
                <td class="right">{{ fmt(e.excess_return_pct_1 as never) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </template>
  <p v-else class="empty">还没有信号报告</p>
</template>
