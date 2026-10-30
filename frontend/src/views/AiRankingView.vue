<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { AiRankingItem, AiRanking, ExportParams } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import AiInputExport from '@/components/AiInputExport.vue'
import ExportButton from '@/components/ExportButton.vue'
import { computed, onMounted, ref } from 'vue'

const dates = ref<string[]>([])
const selected = ref('')
const ranking = ref<AiRanking | null>(null)
const loading = ref(false)
const generating = ref(false)
const progressText = ref('')
const expanded = ref<string | null>(null)
const num = (value: number | null | undefined) => (value == null ? '—' : value.toFixed(1))
const checkTitle = (row: AiRankingItem) => (row.checks ?? []).map((item) => `「${item.claim}」：${item.fact}`).join('\n')
const importOpen = ref(false)
const importText = ref('')
const importDate = ref('')
const importing = ref(false)
const importHint = ref('')

async function doImport() {
  importing.value = true
  importHint.value = ''
  try {
    const saved = await api.importAiRanking(importText.value, importDate.value.trim() || undefined)
    ranking.value = saved
    selected.value = saved.date
    await loadDates()
    const outside = saved.outside_candidates?.length ? `，${saved.outside_candidates.length} 只不在本地前 100 候选里` : ''
    const dropped = saved.dropped_unknown?.length ? `，丢弃本地不存在的代码 ${saved.dropped_unknown.join('、')}` : ''
    importHint.value = `已保存 ${saved.date} 的外部 AI 榜单（${saved.items.length} 只${outside}${dropped}）`
    importText.value = ''
  } catch (reason) {
    importHint.value = reason instanceof ApiError ? reason.detail : '导入失败'
  } finally {
    importing.value = false
  }
}
const poolDays = ref(30)
void api.settings().then((value) => (poolDays.value = value.sample_pool_trading_days ?? 30)).catch(() => undefined)
// 0.3.5 之前生成的榜单没有 pool_days 字段，那时样本池还是旧的 3 日口径
const isOld = computed(() => !!ranking.value && ranking.value.pool_days == null)
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
      if (p.total)
        progressText.value =
          `AI 打分 ${p.done}/${p.total} 批（每批 ${p.batch_size ?? 10} 只，已评 ${p.scored ?? 0} 只）` +
          (p.split ? `，${p.split} 批超时已拆半重试` : '') +
          (p.failed ? `，${p.failed} 批失败` : '')
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
      <AiInputExport />
      <button class="btn btn-ghost" type="button" @click="importOpen = !importOpen">导入外部 AI 结果</button>
    </div>
  </header>

  <section v-if="importOpen" class="card">
    <h2>导入外部 AI 结果</h2>
    <p class="hint">把外部 AI 按导出文件要求返回的 JSON 原样粘进来（带 ```json 围栏或前后说明文字也行）。保存为一份「外部 AI」榜单，第二个交易日的实时监控会照常使用。</p>
    <textarea v-model="importText" rows="8" style="width: 100%; font-family: monospace" placeholder='{"date":"2026-10-30","model":"…","items":[{"code":"600000","score":78,"sub":{…},"risk_deduct":5,"reason":"…","risk":"…","tags":["…"]}]}' />
    <div style="margin-top: 8px; display: flex; gap: 8px; align-items: center">
      <label class="muted">评估日 <input v-model="importDate" placeholder="留空用 JSON 里的 date 或最新交易日" style="width: 16em" /></label>
      <button class="btn" type="button" :disabled="importing || !importText.trim()" @click="doImport">{{ importing ? '导入中…' : '保存为榜单' }}</button>
      <span v-if="importHint" class="hint">{{ importHint }}</span>
    </div>
  </section>

  <p v-if="error" class="alert">{{ error }}</p>
  <section class="card">
    <div class="card-head ai-head">
      <h2 class="nowrap">{{ ranking ? `${ranking.date} 前 20 名` : '前 20 名' }}</h2>
      <span v-if="ranking" class="muted ai-meta">
        候选 {{ ranking.candidate_count }} 只 · 样本池 {{ ranking.pool_size }} 条<template v-if="ranking.skipped_count">（涨停 {{ ranking.limitup_count }}，{{ ranking.skipped_count }} 条缺前 3 日 K 线）</template>（{{ ranking.pool_dates.length }} 个交易日） · 生成于 {{ ranking.run_at.replace('T', ' ').slice(0, 19) }}
        <template v-if="ranking.trigger === 'external'"> · 外部 AI 导入</template><template v-if="ranking.model"> · {{ ranking.model }}</template>
        <template v-if="ranking.snapshot"> · 行情：{{ snapshotLabel[ranking.snapshot.kind] ?? ranking.snapshot.kind }}<template v-if="ranking.snapshot.taken_at && ranking.snapshot.kind !== 'daily'"> {{ ranking.snapshot.taken_at.slice(11, 16) }}</template><template v-if="ranking.snapshot.volume_estimated"> · 成交量按已过 {{ ranking.snapshot.elapsed_minutes }} 分钟换算成全天（估）</template></template>
      </span>
    </div>
    <p v-if="ranking?.unscored_count" class="alert">
      有 {{ ranking.unscored_count }} 只没拿到 AI 评分（{{ ranking.batches?.failed ?? 0 }} 批失败），已保留其余结果。
      <button class="btn btn-ghost" type="button" :disabled="generating" @click="generate()">重新生成</button>
    </p>
    <p v-if="ranking && isOld" class="alert">
      这份榜单是用旧样本池生成的（{{ ranking.pool_dates.length }} 个交易日，{{ ranking.pool_size }} 条），当前设置是近 {{ poolDays }} 个交易日。
      <button class="btn btn-ghost" type="button" :disabled="generating" @click="generate()">用当前样本池重新生成</button>
    </p>
    <Disclaimer :text="ranking?.disclaimer" />
    <p v-if="ranking?.market" class="muted" style="margin: 4px 0">
      市场环境：涨 {{ ranking.market.up }} / 跌 {{ ranking.market.down }}<template v-if="ranking.market.flat != null"> / 平 {{ ranking.market.flat }}</template><template v-if="ranking.market.suspended != null"> / 停牌 {{ ranking.market.suspended }}</template><template v-if="ranking.market.listed"> （共 {{ ranking.market.listed }}）</template><template v-if="ranking.market.flat == null"> / 平盘或停牌 {{ ranking.market.stocks - ranking.market.up - ranking.market.down }}（共 {{ ranking.market.stocks }}）</template> · 涨停 {{ ranking.market.limit_up }} · 跌停 {{ ranking.market.limit_down }} ·
      炸板 {{ ranking.market.broken }}（{{ ranking.market.broken_rate ?? '-' }}%） · 最高 {{ ranking.market.max_height }} 连板
    </p>
    <p v-if="ranking?.check_count" class="hint" style="margin: 4px 0">⚠ {{ ranking.check_count }} 只的标签或理由与本地日线不符，已标「核对」，点行查看。</p>
    <div v-if="loading" class="skeleton" />
    <div v-else-if="!ranking?.items.length" class="empty">还没有 AI 榜单，点「立即生成」试试</div>
    <div v-else class="table-wrap ai-wrap">
      <table class="ai-table">
        <colgroup>
          <col style="width: 44px" /><col style="width: 120px" /><col style="width: 64px" /><col style="width: 100px" />
          <col style="width: 128px" /><col />
        </colgroup>
        <thead>
          <tr>
            <th>排名</th><th>股票</th><th class="right">AI 评分</th><th class="right" title="相似度 = 形态×0.6 + 成交量×0.25 + 量比×0.15">相似度</th>
            <th>最相似涨停</th><th>标签 · 理由（点行展开）</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in ranking.items" :key="row.code">
          <tr class="clickable" :class="{ active: expanded === row.code }" @click="toggle(row.code)">
            <td class="rank" :class="{ top: row.rank <= 3 }">{{ String(row.rank).padStart(2, '0') }}</td>
            <td class="stock">
              <div class="stock-name">{{ row.name }}<span v-if="row.checks?.length" class="check-badge" :title="checkTitle(row)">⚠ 核对</span></div>
              <div class="muted small"><span class="code">{{ row.code }}</span> · {{ row.board_name }}</div>
            </td>
            <td class="right"><b>{{ num(row.ai_score) }}</b></td>
            <td class="right">
              <div>{{ num(row.similarity) }}</div>
              <div class="muted small" title="形态 / 成交量 / 量比">{{ num(row.shape_score) }}/{{ num(row.volume_score) }}/{{ num(row.volume_pct_score) }}</div>
            </td>
            <td class="wrap">
              <template v-if="row.best_match">{{ row.best_match.name }}<div class="muted small code nowrap">{{ row.best_match.code }}</div><div class="muted small code nowrap">{{ row.best_match.limit_date }}</div></template>
              <span v-else class="muted">—</span>
            </td>
            <td class="wrap reason-cell">
              <div v-if="row.tags?.length" class="tag-list"><span v-for="tag in row.tags" :key="tag" class="tag">{{ tag }}</span></div>
              <div class="reason" :class="{ open: expanded === row.code }" :title="row.reason">{{ row.reason }}</div>
            </td>
          </tr>
          <tr v-if="expanded === row.code" class="detail-row">
            <td colspan="6">
              <div class="chips">
                <span v-for="dim in ranking.dimensions ?? DEFAULT_DIMS" :key="dim.key" class="chip">
                  {{ dim.label }}（{{ dim.weight }}%）<b>{{ subText(row.sub?.[dim.key]) }}</b>
                </span>
                <span class="chip" style="color: #dc2626">风险扣分 −{{ (row.risk_deduct ?? 0).toFixed(0) }}</span>
              </div>
              <p v-if="row.risk" class="hint" style="margin: 6px 0 0; color: #b45309">风险提示：{{ row.risk }}</p>
              <ul v-if="row.checks?.length" class="hint check-list">
                <li v-for="item in row.checks" :key="item.claim">⚠ 说法「{{ item.claim }}」与日线不符：{{ item.fact }}</li>
              </ul>
            </td>
          </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.nowrap { white-space: nowrap; }
.ai-head { flex-wrap: wrap; align-items: baseline; justify-content: flex-start; }
.ai-meta { flex: 1 1 420px; line-height: 1.5; }
.ai-wrap { overflow-x: hidden; }
.ai-table { table-layout: fixed; width: 100%; }
.ai-table th, .ai-table td { padding: 8px 8px; vertical-align: top; }
.ai-table td.rank { color: var(--muted); }
.ai-table td.rank.top { color: var(--gold-2); font-weight: 700; }
.small { font-size: 12px; }
.stock-name { white-space: normal; font-weight: 600; }
.check-badge {
  display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 6px; font-size: 11px; font-weight: 600;
  color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.5); background: rgba(251, 191, 36, 0.1); cursor: help;
}
.reason-cell { min-width: 0; }
.tag-list { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px; }
.tag {
  white-space: nowrap; font-size: 12px; line-height: 18px; padding: 1px 8px; border-radius: 999px;
  border: 1px solid var(--line); background: rgba(255, 255, 255, 0.03);
}
.reason { line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; word-break: break-all; }
.reason.open { display: block; -webkit-line-clamp: unset; }
.check-list { margin: 6px 0 0; padding-left: 18px; color: #fbbf24; }
</style>
