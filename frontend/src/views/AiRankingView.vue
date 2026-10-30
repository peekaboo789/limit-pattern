<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { AiRanking, ExportParams } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import ExportButton from '@/components/ExportButton.vue'
import { computed, onMounted, ref } from 'vue'

const dates = ref<string[]>([])
const selected = ref('')
const ranking = ref<AiRanking | null>(null)
const loading = ref(false)
const generating = ref(false)
const progressText = ref('')
const expanded = ref<string | null>(null)
const DEFAULT_DIMS = [
  { key: 'kline', label: 'K线形态', weight: 25 },
  { key: 'volume', label: '量能', weight: 20 },
  { key: 'position', label: '位置与趋势', weight: 15 },
  { key: 'gene', label: '涨停基因', weight: 15 },
  { key: 'similarity', label: '相似度', weight: 15 },
  { key: 'market', label: '市场环境', weight: 10 },
]
function toggle(code: string) {
  expanded.value = expanded.value === code ? null : code
}
function subText(value: number | null | undefined) {
  return value == null ? '缺' : value.toFixed(0)
}
const error = ref('')

const exportParams = computed<ExportParams | null>(() => (ranking.value ? { type: 'ai_ranking', date: ranking.value.date } : null))

async function loadDates() {
  try {
    dates.value = (await api.aiRankingDates()).dates
  } catch (reason) {
    if (!isAbort(reason)) dates.value = []
  }
}

async function open(date?: string) {
  loading.value = true
  error.value = ''
  try {
    ranking.value = await api.aiRanking(date)
    selected.value = ranking.value.date
  } catch (reason) {
    ranking.value = null
    if (!isAbort(reason)) error.value = reason instanceof ApiError ? reason.detail : 'AI 榜单加载失败'
  } finally {
    loading.value = false
  }
}

const snapshotLabel: Record<string, string> = {
  intraday: '盘中实时快照',
  morning_freeze: '11:30 冻结快照',
  close: '收盘报价（正式日线未同步）',
  daily: '正式日线',
}

async function generate(snapshot?: 'morning') {
  generating.value = true
  error.value = ''
  progressText.value = ''
  const timer = window.setInterval(async () => {
    try {
      const p = await api.aiRankingProgress()
      if (p.total) progressText.value = `AI 打分 ${p.done}/${p.total} 批` + (p.failed ? `（${p.failed} 批失败）` : '')
    } catch {
      /* 忽略 */
    }
  }, 2000)
  try {
    ranking.value = await api.generateAiRanking(snapshot ? { snapshot } : {})
    selected.value = ranking.value.date
    await loadDates()
  } catch (reason) {
    if (isAbort(reason)) return
    if (reason instanceof ApiError && reason.status === 409) error.value = '还没配置 AI 接口，请先到「设置」里填写'
    else error.value = reason instanceof ApiError ? reason.detail : '生成失败'
  } finally {
    window.clearInterval(timer)
    generating.value = false
    progressText.value = ''
  }
}

onMounted(async () => {
  await loadDates()
  await open()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>AI 榜单</h1>
      <p class="lede">从当前最相似的前 100 只里，由 AI 按形态和量能打分，留前 20 名。交易日 14:50、15:10 自动生成。</p>
    </div>
    <div class="toolbar">
      <select v-if="dates.length" v-model="selected" @change="open(selected)">
        <option v-for="day in dates" :key="day" :value="day">{{ day }}</option>
      </select>
      <button class="btn" type="button" :disabled="generating" @click="generate()">{{ generating ? (progressText || 'AI 打分中…') : '立即生成' }}</button>
      <button class="btn btn-ghost" type="button" :disabled="generating" title="下午用上午 11:30 收盘时冻结的快照打分" @click="generate('morning')">用 11:30 冻结快照</button>
      <ExportButton :params="exportParams" />
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <section class="card">
    <div class="card-head">
      <h2>{{ ranking ? `${ranking.date} 前 20 名` : '前 20 名' }}</h2>
      <span v-if="ranking" class="muted">
        候选 {{ ranking.candidate_count }} 只 · 样本池 {{ ranking.pool_size }} 条 · 生成于 {{ ranking.run_at.replace('T', ' ').slice(0, 19) }}
        <template v-if="ranking.model"> · {{ ranking.model }}</template>
        <template v-if="ranking.snapshot"> · 行情：{{ snapshotLabel[ranking.snapshot.kind] ?? ranking.snapshot.kind }}<template v-if="ranking.snapshot.taken_at && ranking.snapshot.kind !== 'daily'"> {{ ranking.snapshot.taken_at.slice(11, 16) }}</template><template v-if="ranking.snapshot.volume_estimated"> · 成交量按已过 {{ ranking.snapshot.elapsed_minutes }} 分钟换算成全天（估）</template></template>
      </span>
    </div>
    <Disclaimer :text="ranking?.disclaimer" />
    <p v-if="ranking?.market" class="muted" style="margin: 4px 0">
      市场环境：涨 {{ ranking.market.up }} / 跌 {{ ranking.market.down }} · 涨停 {{ ranking.market.limit_up }} · 跌停 {{ ranking.market.limit_down }} ·
      炸板 {{ ranking.market.broken }}（{{ ranking.market.broken_rate ?? '-' }}%） · 最高 {{ ranking.market.max_height }} 连板
    </p>
    <div v-if="loading" class="skeleton" />
    <div v-else-if="!ranking?.items.length" class="empty">还没有 AI 榜单，点「立即生成」试试</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th><th>代码</th><th>名称</th><th>板块</th>
            <th class="right">AI 评分</th><th class="right">相似度</th><th class="right">形态</th>
            <th class="right">成交量</th><th class="right">量比</th><th>最相似涨停</th><th>标签</th><th>理由</th><th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in ranking.items" :key="row.code">
          <tr class="clickable" @click="toggle(row.code)">
            <td class="rank" :class="{ top: row.rank <= 3 }">{{ String(row.rank).padStart(2, '0') }}</td>
            <td class="code">{{ row.code }}</td>
            <td>{{ row.name }}</td>
            <td class="muted">{{ row.board_name }}</td>
            <td class="right"><b>{{ row.ai_score.toFixed(1) }}</b></td>
            <td class="right">{{ row.similarity.toFixed(1) }}</td>
            <td class="right">{{ row.shape_score.toFixed(1) }}</td>
            <td class="right">{{ row.volume_score.toFixed(1) }}</td>
            <td class="right">{{ row.volume_pct_score.toFixed(1) }}</td>
            <td class="wrap">{{ row.best_match.name }} <span class="muted code">{{ row.best_match.code }} · {{ row.best_match.limit_date }}</span></td>
            <td class="wrap"><span v-for="tag in row.tags ?? []" :key="tag" class="chip" style="margin: 0 4px 2px 0">{{ tag }}</span></td>
            <td class="wrap">{{ row.reason }}</td>
            <td class="muted">{{ expanded === row.code ? '收起' : '详情' }}</td>
          </tr>
          <tr v-if="expanded === row.code" class="detail-row">
            <td colspan="14">
              <div class="chips">
                <span v-for="dim in ranking.dimensions ?? DEFAULT_DIMS" :key="dim.key" class="chip">
                  {{ dim.label }}（{{ dim.weight }}%）<b>{{ subText(row.sub?.[dim.key]) }}</b>
                </span>
                <span class="chip" style="color: #dc2626">风险扣分 −{{ (row.risk_deduct ?? 0).toFixed(0) }}</span>
              </div>
              <p v-if="row.risk" class="hint" style="margin: 6px 0 0; color: #b45309">风险提示：{{ row.risk }}</p>
            </td>
          </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>
