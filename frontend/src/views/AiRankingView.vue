<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { AiRankingItem, AiRanking, ExportParams } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import AiInputExport from '@/components/AiInputExport.vue'
import ExportButton from '@/components/ExportButton.vue'
import { computed, onActivated, onMounted, ref, watch } from 'vue'

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
    // 立即用后端读回的这一天最新榜单刷新（不用页面里缓存的旧榜）
    selected.value = saved.date
    await loadDates()
    ranking.value = await api.aiRanking(saved.date).catch(() => saved)
    sortMode.value = 'external'
    const warned = saved.name_warnings?.length ? `；${saved.name_warnings.length} 只名称与本地不一致，已按代码导入` : ''
    const outside = saved.outside_candidates?.length ? `；${saved.outside_candidates.join('、')} 不在前 100 候选里，已单独计算综合分` : ''
    const rejected = saved.rejected?.length ? `；未导入 ${saved.rejected.map((r) => `${r.code}（${r.reason}）`).join('、')}` : ''
    importHint.value = `已替换 ${saved.date} 榜单：${saved.items.length} 只，按外部 AI 排序${outside}${warned}${rejected}`
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
const INDICATOR_LABELS: Record<string, string> = {
  similarity_score: '三日形态相似度',
  pattern_match_rate: '涨停形态匹配率（不加权）',
  technical_score: '技术形态评分',
  volume_score: '成交量评分',
  capital_score: '资金评分',
  history_limit_up_rate: '历史相似样本成功率',
  risk_score: '风险评分',
}
// 排序：外部导入默认按外部 AI 分（同分保持原顺序），系统生成默认按综合分
const sortMode = ref<'external' | 'composite'>('composite')
const isExternal = computed(() => ranking.value?.trigger === 'external')
watch(
  () => ranking.value?.run_at,
  () => {
    sortMode.value = isExternal.value ? 'external' : 'composite'
  },
  { immediate: true },
)
const rows = computed(() => {
  const items = (ranking.value?.items ?? []).map((row, index) => ({ row, index }))
  // 外部 AI 顺序 = 后端存好的顺序（priority → 外部分 → 给定顺序）
  if (sortMode.value === 'external') items.sort((a, b) => (a.row.rank ?? a.index) - (b.row.rank ?? b.index) || a.index - b.index)
  else items.sort((a, b) => (b.row.composite_score ?? -1) - (a.row.composite_score ?? -1) || a.index - b.index)
  return items.map((item, i) => ({ ...item.row, rank: i + 1 }))
})
function riskLabel(key: string) {
  const map: Record<string, string> = { sentiment: '情绪', up_down_ratio: '涨跌比', median_pct: '中位涨跌%', limit_down: '跌停', limit_up: '涨停', broken_rate: '炸板率%' }
  return map[key] ?? key
}
function scoreLabel(key: string) {
  const base = key.replace(/_score$/, '')
  const known: Record<string, string> = { deepseek: 'DeepSeek', gpt: 'GPT', claude: 'Claude', qwen: 'Qwen', kimi: 'Kimi', glm: 'GLM', doubao: '豆包', gemini: 'Gemini' }
  return known[base.toLowerCase()] ?? base
}
function composite(row: AiRankingItem) {
  if (row.formula && !row.formula.has_signal) return '—'
  return row.composite_score == null ? '—' : row.composite_score.toFixed(2)
}
function levelText(row: AiRankingItem) {
  return row.level || '—'
}
function toggle(code: string) {
  expanded.value = expanded.value === code ? null : code
}
function gapText(gap: number | null | undefined) {
  if (gap == null) return '—'
  return gap > 0 ? `+${gap.toFixed(1)}` : gap.toFixed(1)
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

// 后台已在生成（别的页面点的、或者重启前开始的）：接上进度，不重新开始
let resumeTimer: number | undefined
async function resume() {
  if (generating.value || resumeTimer) return
  const p = await api.aiRankingProgress().catch(() => null)
  if (!p?.running) return
  generating.value = true
  resumeTimer = window.setInterval(async () => {
    const q = await api.aiRankingProgress().catch(() => null)
    if (q?.running) {
      if (q.total) progressText.value = `AI 打分 ${q.done}/${q.total} 批（已评 ${q.scored ?? 0} 只）`
      return
    }
    window.clearInterval(resumeTimer)
    resumeTimer = undefined
    generating.value = false
    progressText.value = ''
    await loadDates()
    await open()
  }, 2000)
}

let mounted = false
onMounted(async () => {
  await loadDates()
  await open()
  await resume()
  mounted = true
})
onActivated(async () => {
  if (!mounted || generating.value) return
  await loadDates()  // 别处生成了新榜单也能看到
  if (!ranking.value || (dates.value[0] && dates.value[0] !== ranking.value.date && selected.value === ranking.value.date)) await open()
  await resume()
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
      <button class="btn" type="button" :disabled="generating" @click="generate()">{{ generating ? (progressText || '计算中…') : '立即生成' }}</button>
      <button class="btn btn-ghost" type="button" :disabled="generating" title="下午用上午 11:30 收盘时冻结的快照打分" @click="generate('morning')">用 11:30 冻结快照</button>
      <ExportButton :params="exportParams" />
      <AiInputExport />
      <button class="btn btn-ghost" type="button" @click="importOpen = !importOpen">导入外部 AI 结果</button>
    </div>
  </header>

  <section v-if="importOpen" class="card">
    <h2>导入外部 AI 结果</h2>
    <p class="hint">把外部 AI 按导出文件要求返回的 JSON 原样粘进来（带 ```json 围栏或前后说明文字也行）。系统按固定公式给这些票算综合评分并排序，外部给的 score 记为模型分并显示偏差。保存为一份「外部 AI」榜单，第二个交易日的实时监控会照常使用。</p>
    <textarea v-model="importText" rows="8" style="width: 100%; font-family: monospace" placeholder='{"date":"2026-10-30","model":"…","items":[{"code":"600000","score":72,"reason":"…","risk":"…","tags":["…"]}]}' />
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
      <label v-if="ranking?.items.length" class="muted sort-toggle">排序：
        <select v-model="sortMode">
          <option value="external">外部 AI</option>
          <option value="composite">系统综合分</option>
        </select>
      </label>
    </div>
    <div v-if="ranking?.external_meta" class="ext-meta">
      <b v-if="ranking.external_meta.title">{{ ranking.external_meta.title }}</b>
      <p v-if="ranking.external_meta.market_sentiment" class="muted" style="margin: 4px 0">{{ ranking.external_meta.market_sentiment }}</p>
      <p v-if="ranking.external_meta.market_risk" class="muted small" style="margin: 4px 0">外部 AI 市场判断：
        <template v-for="(v, k) in ranking.external_meta.market_risk" :key="k">{{ riskLabel(String(k)) }} {{ v }}；</template>
      </p>
      <p v-if="ranking.external_meta.disclaimer" class="hint" style="margin: 4px 0">{{ ranking.external_meta.disclaimer }}</p>
    </div>
    <p v-if="ranking?.name_warnings?.length" class="hint">⚠ {{ ranking.name_warnings.length }} 只名称与本地不一致（{{ ranking.name_warnings.map((w) => `${w.code} 写成「${w.claimed}」，本地「${w.local}」`).join('；') }}），已按代码导入。</p>
    <p v-if="ranking?.unscored_count" class="alert">
      有 {{ ranking.unscored_count }} 只没拿到 AI 解读（{{ ranking.batches?.failed ?? 0 }} 批失败）；排名按系统综合评分，不受影响。
      <button class="btn btn-ghost" type="button" :disabled="generating" @click="generate()">重新生成</button>
    </p>
    <p v-if="ranking?.ai_error" class="hint">{{ ranking.ai_error }}</p>
    <p v-if="ranking?.formula_text" class="hint" style="margin: 4px 0">{{ ranking.formula_text }} <template v-if="isExternal">这是外部 AI 导入的榜单：默认按外部 AI 的分数排名，系统综合分作对照；次日监控用这 {{ ranking.items.length }} 只。</template><template v-else>排名只看系统综合评分，AI 只给依据和对照分。</template></p>
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
          <col style="width: 40px" /><col style="width: 112px" /><col style="width: 78px" />
          <col style="width: 50px" /><col style="width: 50px" /><col style="width: 50px" /><col style="width: 50px" /><col style="width: 50px" /><col style="width: 50px" />
          <col style="width: 70px" /><col style="width: 56px" /><col style="width: 60px" /><col />
        </colgroup>
        <thead>
          <tr>
            <th>排名</th><th>股票</th>
            <th class="right" :title="ranking.formula_text || '系统综合评分'">{{ isExternal ? '外部AI分' : '综合评分' }}</th>
            <th class="right" title="形态贡献（权重 35%，读最高相似度）">形态</th>
            <th class="right" title="技术贡献（20%）">技术</th>
            <th class="right" title="量能贡献（15%）">量能</th>
            <th class="right" title="资金贡献（15%）：没有真实资金流数据，恒缺，权重摊给其余项">资金</th>
            <th class="right" title="历史成功率贡献（15%）：高度相似样本次日继续涨停的已发生频率">历史</th>
            <th class="right" title="风险分 ≥80 扣 20、≥60 扣 10、≥40 扣 5">风险</th>
            <th class="right" title="模型自评分（只作对照）与系统综合评分的差：正数 = 模型给得更高">模型分/差</th>
            <th class="right" title="预测分 = 0.5×最高相似度 + 0.3×相似度加权连板率 + 0.2×档位倍率（2 倍封顶），缺项摊权重">预测分</th>
            <th class="right" title="最高相似度（选前 100 用它）">相似度</th>
            <th>模型依据（点行展开）</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in rows" :key="row.code">
          <tr class="clickable" :class="{ active: expanded === row.code }" @click="toggle(row.code)">
            <td class="rank" :class="{ top: row.rank <= 3 }">{{ String(row.rank).padStart(2, '0') }}</td>
            <td class="stock">
              <div class="stock-name">{{ row.name }}<span v-if="row.name_warning" class="check-badge" :title="row.name_warning">⚠ 名称</span><span v-if="row.checks?.length" class="check-badge" :title="checkTitle(row)">⚠ 核对</span></div>
              <div class="muted small"><span class="code">{{ row.code }}</span> · {{ row.board_name }}<template v-if="row.is_limit_up"> · 已涨停</template></div>
            </td>
            <td v-if="isExternal" class="right">
              <b class="ext-score">{{ num(row.model_score) }}</b>
              <div class="muted small">综合 {{ composite(row) }} · 差 {{ gapText(row.model_gap) }}</div>
              <div v-for="(v, k) in row.extra_scores ?? {}" :key="k" class="muted small">{{ scoreLabel(String(k)) }} {{ v }}</div>
              <div v-if="row.priority != null" class="muted small">优先级 {{ row.priority }}</div>
            </td>
            <td v-else class="right">
              <b>{{ composite(row) }}</b>
              <div class="muted small">{{ levelText(row) }}</div>
            </td>
            <td class="right">{{ num(row.contributions?.pattern) }}</td>
            <td class="right">{{ num(row.contributions?.technical) }}</td>
            <td class="right">{{ num(row.contributions?.volume) }}</td>
            <td class="right">{{ num(row.contributions?.capital) }}</td>
            <td class="right">{{ num(row.contributions?.history) }}</td>
            <td class="right" :style="{ color: row.risk_penalty ? '#dc2626' : undefined }">{{ row.risk_penalty == null ? '—' : row.risk_penalty ? `−${row.risk_penalty}` : '0' }}</td>
            <td class="right">
              <div>{{ num(row.model_score) }}</div>
              <div class="muted small">{{ gapText(row.model_gap) }}</div>
            </td>
            <td class="right">{{ num(row.prediction_score) }}</td>
            <td class="right">
              <div>{{ num(row.max_similarity ?? row.similarity) }}</div>
              <div v-if="row.sample_count" class="muted small" title="高度相似样本数 / 比对样本数">{{ row.high_similarity_count }}/{{ row.sample_count }}</div>
            </td>
            <td class="wrap reason-cell">
              <div v-if="row.tags?.length" class="tag-list"><span v-for="tag in row.tags" :key="tag" class="tag">{{ tag }}</span></div>
              <div class="reason" :class="{ open: expanded === row.code }" :title="row.reason">{{ row.reason || '—' }}</div>
            </td>
          </tr>
          <tr v-if="expanded === row.code" class="detail-row">
            <td colspan="13">
              <div v-if="row.formula" class="chips">
                <span v-for="(value, key) in row.formula.indicators" :key="key" class="chip">{{ INDICATOR_LABELS[key] ?? key }} <b>{{ num(value) }}</b></span>
                <span class="chip">有数权重 <b>{{ row.formula.weight_total }}</b></span>
                <span v-if="row.formula.missing_items?.length" class="chip">缺项 <b>{{ row.formula.missing_items.join('、') }}</b></span>
              </div>
              <div v-else class="chips">
                <span class="chip">这份榜单是 0.4.0 以前生成的，没有系统综合评分</span>
              </div>
              <p class="hint" style="margin: 6px 0 0">
                单股历史总分：<template v-if="row.pick_history && row.pick_history.picks">{{ row.pick_history.total ?? '—' }}（上榜 {{ row.pick_history.picks }} 次，次日涨停 {{ row.pick_history.hit }}/{{ row.pick_history.hit + row.pick_history.miss }}，命中率 {{ row.pick_history.hit_rate == null ? '—' : row.pick_history.hit_rate + '%' }}，待定 {{ row.pick_history.pending }}）</template><template v-else>—（还没有已定判的上榜记录）</template>
                <template v-if="row.best_match"> · 最相似涨停 {{ row.best_match.name }} {{ row.best_match.code }} @ {{ row.best_match.limit_date }}</template>
              </p>
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
.ext-meta { border: 1px solid rgba(160, 186, 224, 0.18); border-radius: 8px; padding: 8px 12px; margin: 6px 0; }
.ext-score { color: var(--gold-2); font-size: 1.1em; }
.sort-toggle { margin-left: auto; white-space: nowrap; }
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
