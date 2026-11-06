<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import { request } from '@/api/http'
import type { CouncilBar, CouncilEvidence, CouncilSession, CouncilProgress, CouncilWebEvidence } from '@/api/types'
import KlineChart from '@/components/KlineChart.vue'
import { computed, onMounted, ref } from 'vue'

interface Prof { id: string; name: string; model: string; api_key_set: boolean; active: boolean }

const stock = ref('')
const date = ref('')
const sessions = ref<CouncilSession[]>([])
const profiles = ref<Prof[]>([])
const order = ref<string[]>([])
const loading = ref(false)
const starting = ref(false)
const error = ref('')
const hint = ref('')
const progress = ref<CouncilProgress | null>(null)
const expanded = ref<number | null>(null)
const polling = ref<number | null>(null)

const keyProfiles = computed(() => profiles.value.filter((p) => p.api_key_set))
const selected = computed(() => order.value.filter((id) => keyProfiles.value.some((p) => p.id === id)))
const canStart = computed(() => !!stock.value.trim() && selected.value.length >= 2 && !starting.value)

async function loadProfiles() {
  const data = await request<{ items: Prof[] }>('/api/ai/profiles')
  profiles.value = data.items
}

async function loadOrder() {
  const data = await api.councilProfileOrder()
  order.value = data.profile_ids.filter((id) => keyProfiles.value.some((p) => p.id === id))
}

async function loadSessions() {
  loading.value = true
  try {
    const data = await api.councilLatest()
    sessions.value = data.items
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '讨论记录加载失败'
  } finally {
    loading.value = false
  }
}

function toggleProfile(id: string) {
  if (!keyProfiles.value.some((p) => p.id === id)) return
  if (order.value.includes(id)) order.value = order.value.filter((x) => x !== id)
  else order.value = [...order.value, id]
}

function move(id: string, dir: -1 | 1) {
  const index = order.value.indexOf(id)
  const next = index + dir
  if (index < 0 || next < 0 || next >= order.value.length) return
  const copy = [...order.value]
  ;[copy[index], copy[next]] = [copy[next], copy[index]]
  order.value = copy
}

async function saveOrder() {
  hint.value = ''
  try {
    const data = await api.councilSaveProfileOrder(order.value)
    order.value = data.profile_ids
    hint.value = '已保存默认开会顺序'
  } catch (reason) {
    hint.value = reason instanceof ApiError ? reason.detail : '保存失败'
  }
}

async function start() {
  if (!canStart.value) return
  starting.value = true
  error.value = ''
  hint.value = ''
  try {
    const result = await api.councilRun({ code: stock.value.trim(), date: date.value.trim() || undefined, profile_ids: order.value })
    hint.value = `已开始讨论 #${result.session_id}，正在联网核对`
    await beginPolling()
    await loadSessions()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '开始失败'
  } finally {
    starting.value = false
  }
}

async function beginPolling() {
  if (polling.value !== null) return
  polling.value = window.setInterval(async () => {
    try {
      progress.value = await api.councilProgress()
      if (progress.value && !progress.value.running) {
        stopPolling()
        await loadSessions()
      }
    } catch {
      /* 忽略轮询错误 */
    }
  }, 1500)
}

function stopPolling() {
  if (polling.value !== null) {
    window.clearInterval(polling.value)
    polling.value = null
  }
}

function confidenceLabel(value: string | null | undefined) {
  return value ? ({ low: '低', medium: '中', high: '高' } as Record<string, string>)[value] ?? value : '—'
}

function open(row: CouncilSession) {
  expanded.value = expanded.value === row.id ? null : row.id
}

function joinList(values: string[] | undefined) {
  return values?.length ? values.join('；') : '—'
}

function categoryLabel(category: string | undefined) {
  return category ? ({ market: '行情与图形', market_intraday: '分时/分钟', capital: '资金与交易', trade: '大宗/回购/增减持/解禁', news: '资讯快讯', announcement: '公司公告', research: '研报观点', financial: '最新财报与财务分析' } as Record<string, string>)[category] ?? category : '其他'
}

function statusLabel(status: string) {
  return status === 'ok' ? '已取到' : status === 'empty' ? '未取到' : '失败'
}

function groupedWeb(evidence: CouncilEvidence | null | undefined): [string, CouncilWebEvidence[]][] {
  const groups = new Map<string, CouncilWebEvidence[]>()
  for (const item of evidence?.web ?? []) {
    const key = item.category ?? 'other'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(item)
  }
  return [...groups.entries()].sort((a, b) => (a[0] === 'market' ? -1 : b[0] === 'market' ? 1 : a[0].localeCompare(b[0])))
}

function chartBars(evidence: CouncilEvidence | null | undefined): CouncilBar[] {
  return evidence?.local?.chart?.bars ?? evidence?.local?.bars_30d ?? []
}

function digestText(digest: unknown): string {
  if (digest == null) return '—'
  if (typeof digest === 'string') return digest
  if (Array.isArray(digest)) return digest.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))).join('；')
  return JSON.stringify(digest, null, 2)
}

onMounted(async () => {
  await loadProfiles()
  await loadOrder()
  await loadSessions()
  progress.value = await api.councilProgress().catch(() => null)
  if (progress.value?.running) await beginPolling()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>多模型讨论验证</h1>
      <p class="lede">系统先做本地证据 + 联网核对，再由多个模型顺序发言，最后一位主席估算下一交易日收盘上涨概率。</p>
    </div>
    <button class="btn btn-ghost" type="button" :disabled="loading" @click="loadSessions">刷新</button>
  </header>

  <p class="hint">这是讨论会估计，不计入综合评分，不是涨停概率，不是买入建议。概率指下一交易日收盘相对评估日收盘上涨的估计。</p>

  <section class="card">
    <div class="card-head"><h2>发起讨论</h2></div>
    <div class="filters">
      <label class="field">
        <span>股票代码 / 名称</span>
        <input v-model="stock" placeholder="600519 或 茅台" />
      </label>
      <label class="field">
        <span>评估日（默认最新数据日）</span>
        <input v-model="date" type="date" />
      </label>
    </div>
    <div v-if="keyProfiles.length < 2" class="hint">至少需要两个已填密钥的模型。请到「设置 → AI 模型」配置。</div>
    <div v-else>
      <div class="hint">勾选参会模型并排序，最后一名将作为主席，总结并估算次日上涨概率。</div>
      <div class="council-models">
        <div v-for="p in keyProfiles" :key="p.id" class="council-model" :class="{ on: order.includes(p.id) }">
          <label class="check-row">
            <input type="checkbox" :checked="order.includes(p.id)" @change="toggleProfile(p.id)" />
            <span>{{ p.name || p.model }}（{{ p.model }}）</span>
            <span v-if="order.includes(p.id)" class="muted">第 {{ order.indexOf(p.id) + 1 }} 位{{ order.indexOf(p.id) === order.length - 1 ? ' · 总结主席' : '' }}</span>
          </label>
          <span v-if="order.includes(p.id)" class="ops">
            <button class="btn btn-ghost" type="button" :disabled="order.indexOf(p.id) === 0" @click="move(p.id, -1)">上移</button>
            <button class="btn btn-ghost" type="button" :disabled="order.indexOf(p.id) === order.length - 1" @click="move(p.id, 1)">下移</button>
          </span>
        </div>
      </div>
      <div class="toolbar" style="margin-top: 10px">
        <button class="btn" type="button" :disabled="!canStart" @click="start">{{ starting ? '启动中…' : '开始讨论' }}</button>
        <button class="btn btn-ghost" type="button" @click="saveOrder">保存默认开会顺序</button>
      </div>
    </div>
    <p v-if="hint" class="hint" style="color: #16a34a">{{ hint }}</p>
    <p v-if="error" class="alert">{{ error }}</p>
    <div v-if="progress?.running" class="hint">
      进度：{{ progress.stage }}（{{ progress.done }}/{{ progress.total }}）<template v-if="progress.current_model"> · 当前：{{ progress.current_model }}</template>
      <template v-if="progress.last_status"> · {{ progress.last_status }}</template>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>讨论记录</h2><span class="muted">{{ sessions.length }} 场</span></div>
    <div v-if="loading" class="skeleton" />
    <div v-else-if="!sessions.length" class="empty">还没有讨论记录</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr><th>代码</th><th>名称</th><th>评估日</th><th class="right">模型数</th><th class="right">次日上涨概率</th><th>信心</th><th>状态</th><th>时间</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="row in sessions" :key="row.id" tabindex="0" @click="open(row)" @keydown.enter="open(row)">
            <td class="code">{{ row.stock_code }}</td>
            <td>{{ row.stock_name }}</td>
            <td>{{ row.eval_date }}</td>
            <td class="right">{{ row.participants.length }}</td>
            <td class="right"><b>{{ row.conclusion?.up_probability == null ? '—' : `${row.conclusion.up_probability.toFixed(1)}%` }}</b></td>
            <td>{{ confidenceLabel(row.conclusion?.confidence) }}</td>
            <td>{{ row.status === 'done' ? '完成' : row.status === 'running' ? '进行中' : '失败' }}</td>
            <td class="muted">{{ row.created_at.slice(0, 19).replace('T', ' ') }}</td>
            <td class="right"><button class="btn btn-ghost" type="button" @click.stop="open(row)">{{ expanded === row.id ? '收起' : '详情' }}</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-for="row in sessions.filter((item) => expanded === item.id)" :key="'detail-' + row.id" class="council-detail">
      <h3>{{ row.stock_name }} {{ row.stock_code }} · {{ row.eval_date }}</h3>
      <div v-if="row.error" class="alert">{{ row.error }}</div>
      <template v-if="row.conclusion">
        <p class="hint">下一交易日收盘上涨概率：<b>{{ row.conclusion.up_probability.toFixed(1) }}%</b>，信心：{{ confidenceLabel(row.conclusion.confidence) }}。{{ row.conclusion.disclaimer }}</p>
        <p class="hint">综述：{{ row.conclusion.summary || '—' }}</p>
        <div class="detail-grid">
          <div><b>支持上涨</b><p>{{ joinList(row.conclusion.bullish) }}</p></div>
          <div><b>不支持上涨</b><p>{{ joinList(row.conclusion.bearish) }}</p></div>
          <div><b>共识</b><p>{{ joinList(row.conclusion.agreements) }}</p></div>
          <div><b>分歧</b><p>{{ joinList(row.conclusion.disagreements) }}</p></div>
          <div><b>风险</b><p>{{ joinList(row.conclusion.risks) }}</p></div>
          <div><b>CNE6 因子</b><p>{{ joinList(row.conclusion.cne6_notes) }}</p></div>
        </div>
      </template>
      <h4>发言记录</h4>
      <div v-for="statement in row.statements" :key="statement.index" class="statement">
        <b>{{ statement.speaker }}</b> <span class="muted">{{ statement.model }}</span>
        <p v-if="statement.error" class="alert">{{ statement.error }}</p>
        <pre v-else>{{ statement.content }}</pre>
      </div>
      <h4>证据包</h4>
      <div v-if="chartBars(row.evidence).length" class="chart-box">
        <div class="chart-head">
          <span>近 30 个交易日 K 线</span>
          <span class="muted">{{ row.evidence.local?.chart?.source || '本地数据' }} · {{ chartBars(row.evidence).length }} 根</span>
        </div>
        <KlineChart :bars="chartBars(row.evidence) as any" :height="340" />
      </div>
      <div v-else class="hint">没有可展示的 K 线数值序列。</div>

      <div v-for="[category, items] in groupedWeb(row.evidence)" :key="category" class="evidence-group">
        <h5>{{ categoryLabel(category) }} <span class="muted">{{ items.length }} 源</span></h5>
        <div v-for="(item, itemIndex) in items" :key="itemIndex" class="evidence-item">
          <div class="evidence-item-head">
            <b>{{ item.source }}</b>
            <span class="status" :class="item.status">{{ statusLabel(item.status) }}</span>
            <span class="muted">{{ item.fetched_at?.slice(0, 19).replace('T', ' ') || '—' }}</span>
          </div>
          <div v-if="item.window" class="muted">窗口：{{ item.window.start }} 至 {{ item.window.end }}（{{ item.window.days }} 天）</div>
          <pre class="evidence">{{ digestText(item.digest) }}</pre>
        </div>
      </div>

      <details>
        <summary>完整证据 JSON</summary>
        <pre class="evidence">{{ JSON.stringify(row.evidence, null, 2) }}</pre>
      </details>
    </div>
  </section>
</template>

<style scoped>
.council-models { display: grid; gap: 6px; margin-top: 8px; }
.council-model { display: flex; justify-content: space-between; align-items: center; padding: 6px 8px; border: 1px solid rgba(127,127,127,0.25); border-radius: 4px; }
.council-model.on { border-color: #e8b960; background: rgba(232,185,96,0.06); }
.check-row { display: flex; gap: 8px; align-items: center; }
.ops { display: flex; gap: 6px; }
.council-detail { margin-top: 14px; padding-top: 12px; border-top: 1px dashed rgba(127,127,127,0.3); }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin: 10px 0; }
.detail-grid p { white-space: pre-wrap; font-size: 13px; }
.statement { padding: 8px 0; border-bottom: 1px solid rgba(127,127,127,0.18); }
pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; margin: 6px 0 0; }
.evidence { max-height: 300px; overflow: auto; background: rgba(0,0,0,0.02); padding: 8px; }
.chart-box { margin: 10px 0; padding: 8px; border: 1px solid rgba(127,127,127,0.25); border-radius: 4px; }
.chart-head { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; margin-bottom: 6px; }
.evidence-group { margin: 12px 0; }
.evidence-group h5 { margin: 0 0 6px; font-size: 14px; }
.evidence-item { padding: 8px; margin-bottom: 8px; background: rgba(0,0,0,0.02); border-radius: 4px; }
.evidence-item-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.status { padding: 1px 5px; border-radius: 3px; font-size: 11px; }
.status.ok { background: rgba(34,197,94,0.15); color: #16a34a; }
.status.empty { background: rgba(234,179,8,0.12); color: #ca8a04; }
.status.failed { background: rgba(239,68,68,0.12); color: #dc2626; }
</style>
