<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { CouncilBar, CouncilEvidence, CouncilSession, CouncilProgress, CouncilWebEvidence } from '@/api/types'
import KlineChart from '@/components/KlineChart.vue'
import CouncilSourcesPanel from '@/components/CouncilSourcesPanel.vue'
import CouncilMessageContent from '@/components/CouncilMessageContent.vue'
import { chatMessages, transcriptItems, publicContent, councilIdentity, createChatScroll, shouldSendKey, clearSubmittedDraft, currentStage, conversationStatus } from '@/composables/councilChat'
import { useCouncilHistory, createCouncilPoll } from '@/composables/useCouncilHistory'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

interface Prof { id: string; name: string; model: string; api_key_set: boolean; active: boolean }

const stock = ref('')
const date = ref('')
const profiles = ref<Prof[]>([])
const order = ref<string[]>([])
const starting = ref(false)
const hint = ref('')
const progress = ref<CouncilProgress | null>(null)
const speakingFor = ref<number | null>(null)
const selectedSpeaker = ref<Record<number, string>>({})
const userInput = ref<Record<string, string>>({})
const composing = ref<Record<string, boolean>>({})
const compositionEnded = new Map<string, number>()
const speakingBusy = ref<Record<number, boolean>>({})
const detailHint = ref<Record<number, string>>({})
const evidenceOpen = ref<Record<string, boolean>>({})

const history = useCouncilHistory(api)
const { sessions, expanded, opening, loading, error, detailError, detailLoading, detailReady, deleting, deleteError, loadSessions, loadSession, replaceSession } = history
let alive = true
let pollFailures = 0
const poller = createCouncilPoll(async (current) => {
  try {
    const next = await api.councilProgress()
    if (!current() || !alive) return false
    progress.value = next.session_id && history.isDeleted(next.session_id) ? null : next
    pollFailures = 0
    const ids = new Set([expanded.value, next.session_id])
    for (const id of ids) if (id && current() && !history.isDeleted(id)) await loadSession(id)
    return !!next.running || speakingFor.value !== null
  } catch (reason) {
    if (!current() || !alive) return false
    error.value = reason instanceof ApiError ? reason.detail : '自动刷新失败，请点击刷新/重试'
    return ++pollFailures < 3
  }
})
function beginPolling() { if (!alive) return; pollFailures = 0; poller.start() }
function stopPolling() { poller.stop() }
async function refresh() { await loadSessions(); if (!alive) return; if (expanded.value) await loadSession(expanded.value); beginPolling() }
async function open(row: CouncilSession) {
  await history.open(row)
  if (alive && expanded.value === row.id && (row.status === 'running' || progress.value?.running)) beginPolling()
}
async function retryDetail(id: number) { await loadSession(id); if (alive) beginPolling() }
async function removeSession(row: CouncilSession) {
  if (speakingBusy.value[row.id] || row.status === 'running') return
  const removed = await history.deleteSession(row.id, () => window.confirm(`确定永久删除 ${row.stock_name} ${row.stock_code}（${row.eval_date}）的讨论 #${row.id}？对话与证据不可恢复。`))
  if (removed) {
    delete userInput.value[draftKey(row)]; delete evidenceOpen.value[draftKey(row)]; chatScroll.remove(draftKey(row)); delete selectedSpeaker.value[row.id]
    delete detailHint.value[row.id]; delete speakingBusy.value[row.id]
    if (progress.value?.session_id === row.id) progress.value = null
    stopPolling(); beginPolling()
  }
}

const keyProfiles = computed(() => profiles.value.filter((p) => p.api_key_set))
const selected = computed(() => order.value.filter((id) => keyProfiles.value.some((p) => p.id === id)))
const canStart = computed(() => !!stock.value.trim() && selected.value.length >= 2 && !starting.value && !progress.value?.running)

const draftKey = councilIdentity
const readyFor = (row:CouncilSession) => opening.value!==row.id && detailReady.value[row.id]===(row.created_at || 'legacy')
const busyFor = (row:CouncilSession) => row.status==='running' || !!speakingBusy.value[row.id] || !!progress.value?.running
const activeRow = computed(() => sessions.value.find(row => row.id === expanded.value))
const activeKey = computed(() => activeRow.value ? draftKey(activeRow.value) : '')
const chatScroll = createChatScroll()
const unread = ref(0)
const following = ref(true)
let chatViewport: HTMLElement | null = null
let restoring = false
const anchors = new Map<string, {key:string;offset:number}>()
function bindChatViewport(element: unknown) { chatViewport = element instanceof HTMLElement ? element : null }
function trackScroll() {
  if (!chatViewport || !activeKey.value || restoring || opening.value===expanded.value) return
  const el=chatViewport,key=activeKey.value
  chatScroll.scroll(key,el.scrollHeight,el.scrollTop,el.clientHeight)
  const rect=el.getBoundingClientRect()
  const anchor=[...el.querySelectorAll<HTMLElement>('[data-message-key]')].find(item=>item.getBoundingClientRect().bottom>rect.top)
  if(anchor)anchors.set(key,{key:anchor.dataset.messageKey || '',offset:anchor.getBoundingClientRect().top-rect.top})
  following.value=chatScroll.state(key).follow;unread.value=chatScroll.state(key).unread
}
function restoreScroll(key:string) {
  if(!chatViewport || activeKey.value!==key || opening.value===expanded.value)return
  const el=chatViewport,state=chatScroll.state(key),anchor=anchors.get(key)
  restoring=true
  if(state.follow)el.scrollTop=el.scrollHeight
  else {
    el.scrollTop=state.top
    const found=anchor&&[...el.querySelectorAll<HTMLElement>('[data-message-key]')].find(item=>item.dataset.messageKey===anchor.key)
    if(found&&anchor)el.scrollTop+=found.getBoundingClientRect().top-el.getBoundingClientRect().top-anchor.offset
  }
  following.value=state.follow;unread.value=state.unread
  restoring=false
}
function goBottom() { if(!activeKey.value || !chatViewport)return;chatScroll.bottom(activeKey.value);chatViewport.scrollTop=chatViewport.scrollHeight;trackScroll() }
watch(activeKey,key=>{const state=key?chatScroll.state(key):null;following.value=state?.follow ?? true;unread.value=state?.unread ?? 0},{flush:'sync'})
watch([activeKey, opening], async () => { const key=activeKey.value;await nextTick();if(alive&&key)restoreScroll(key) })
watch(() => activeRow.value ? chatMessages(activeRow.value).map(item=>`${item.key}:${item.content}:${item.error || ''}`).join('|') : '', async () => {
  const key=activeKey.value,row=activeRow.value;if(!key||!row)return
  chatScroll.messages(key,chatMessages(row).filter(item=>item.kind!=='system'&&(item.content||item.error)).map(item=>item.key))
  await nextTick();if(alive)restoreScroll(key)
}, {flush:'post'})
function endComposition(row:CouncilSession) {const key=draftKey(row);composing.value[key]=false;compositionEnded.set(key,Date.now())}
function composerKey(event:KeyboardEvent,row:CouncilSession) {
  const key=draftKey(row)
  if(!shouldSendKey(event,!!composing.value[key],Date.now()-(compositionEnded.get(key)||0)))return
  if(!readyFor(row)||busyFor(row))return // Busy Enter remains a newline in the draft.
  event.preventDefault();void addUserMessage(row.id)
}
function emptyConversation(row:CouncilSession) {
  if(row.status==='running')return `${row.progress?.stage || '正在准备证据'}；尚无模型发言，请查看阶段状态。`
  return row.status==='failed'?'本场尚未取得发言；失败原因已显示，可在会话空闲后点名续轮。':'暂无发言，可输入意见或点名模型开始对话。'
}
async function askChair(row: CouncilSession) { const chair = row.participants[row.participants.length - 1]; if (!chair) return; selectedSpeaker.value[row.id] = chair.id; await askSpeaker(row.id) }

async function loadProfiles() {
  const data = await api.councilModels()
  if (alive) profiles.value = data.items
}

async function loadOrder() {
  const data = await api.councilProfileOrder()
  if (alive) order.value = data.profile_ids.filter((id) => keyProfiles.value.some((p) => p.id === id))
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
    if (!alive) return
    stopPolling()
    history.acceptNewSession(result.session_id)
    hint.value = `已开始讨论 #${result.session_id}，正在联网核对`
    expanded.value = result.session_id
    opening.value = result.session_id
    await loadSession(result.session_id) // Never wait for a potentially large history response first.
    if (!alive) return
    beginPolling()
    void loadSessions()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '开始失败'
  } finally {
    starting.value = false
  }
}

async function reconcileStale() {
  try {
    const result = await api.councilReconcileStale()
    await refresh()
    hint.value = result.terminated ? `已终止 ${result.terminated} 场遗留运行中会话` : '没有遗留运行中会话'
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '终止遗留会话失败'
  }
}

function probabilityText(row: CouncilSession) {
  const result = row.conclusion
  if (!result) return '—'
  if (result.probability_status === 'estimated' && result.probability_unit === 'percent' && result.up_probability != null && Number.isFinite(result.up_probability)) return `约 ${result.up_probability.toFixed(1)}%（主观估计）`
  if (result.probability_status === 'not_estimable') return '模型未提供估计'
  return result.probability_status === 'ambiguous_unit' ? '单位不明 · 不可确定' : result.probability_status === 'invalid' ? '输出无效 · 不可估计' : '证据不足 / 不可估计'
}

function confidenceLabel(value: string | null | undefined) {
  return value ? ({ low: '低', medium: '中', high: '高' } as Record<string, string>)[value] ?? value : '—'
}

function joinList(values: string[] | undefined) {
  return values?.length ? values.join('；') : '—'
}

function categoryLabel(category: string | undefined) {
  return category ? ({ market: '行情与图形', market_intraday: '分时/分钟', capital: '资金与交易', trade: '大宗/回购/增减持/解禁', news: '资讯快讯', announcement: '公司公告', research: '研报观点', financial: '最新财报与财务分析' } as Record<string, string>)[category] ?? category : '其他'
}

function statusLabel(status: string) {
  return ({ ok: '已取到', empty: '有效查询无记录', failed: '接口故障', not_applicable: '已核实不适用', unavailable: '公开数据不可得', unverified: '尚未核验' } as Record<string, string>)[status] || status
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
  return evidence?.kline?.chart?.bars ?? evidence?.local?.chart?.bars ?? evidence?.local?.bars_30d ?? []
}

function evidenceSummary(evidence: CouncilEvidence | null | undefined): string {
  if (!evidence?.kline) return ''
  const kline = evidence.kline
  const reason = kline.missing_reason || ''
  const summary = `本地 ${kline.local_count} 根 / 公开 ${kline.public_count} 根，预期约 ${kline.expected_trading_days ?? 30} 个交易日`
  return reason ? `${summary}。${reason}` : summary
}

function displayTime(value: string | null | undefined): string {
  if (!value) return '—'
  const instant = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(instant.getTime()) ? value : instant.toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai', hour12: false })
}

function durationText(row: CouncilSession): string {
  if (row.duration_seconds == null) return ''
  const seconds = Math.round(row.duration_seconds)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${seconds % 60} 秒`
}

function statusText(row: CouncilSession) { return conversationStatus(row) }

async function addUserMessage(id: number) {
  if (opening.value===id || speakingBusy.value[id] || progress.value?.running) return
  const row=sessions.value.find(item=>item.id===id)
  if(!row||!readyFor(row)||busyFor(row))return
  const key=draftKey(row),submitted=userInput.value[key] || ''
  const text = submitted.trim()
  if (!text) {
    detailError.value[id] = '意见不能为空'
    return
  }
  if (text.length > 4000) {
    detailError.value[id] = '意见最多 4000 字'
    return
  }
  speakingBusy.value[id] = true
  detailError.value[id] = ''
  detailHint.value[id] = ''
  try {
    const updated = await api.councilUserMessage(id, text)
    if (!alive) return
    if(updated.id!==id)throw new Error('响应会话不匹配')
    replaceSession(updated)
    userInput.value[key] = clearSubmittedDraft(userInput.value[key] || '',submitted)
    detailHint.value[id] = '用户意见已写入对话'
  } catch (reason) {
    if (alive) detailError.value[id] = reason instanceof ApiError && reason.status < 500 ? reason.detail : `${reason instanceof ApiError ? reason.detail : '保存请求未获确认'}；请先重试加载确认是否已写入，再决定是否重发。草稿已保留。`
  } finally {
    if (alive) speakingBusy.value[id] = false
  }
}

async function askSpeaker(id: number) {
  if (opening.value===id || speakingBusy.value[id] || progress.value?.running) return
  const row=sessions.value.find(item=>item.id===id)
  if(!row||!readyFor(row)||busyFor(row))return
  const profileId = selectedSpeaker.value[id]
  if (!profileId) {
    detailError.value[id] = '请选择要发言的模型'
    return
  }
  speakingBusy.value[id] = true
  speakingFor.value = id
  await beginPolling()
  detailError.value[id] = ''
  detailHint.value[id] = ''
  try {
    const updated = await api.councilSpeak(id, profileId)
    if (!alive) return
    if(updated.id!==id)throw new Error('响应会话不匹配')
    replaceSession(updated)
    await loadSession(id)
    if (!alive) return
    detailHint.value[id] = updated.status === 'failed' ? '模型发言失败，详见对话记录' : '模型已完成发言'
  } catch (reason) {
    if (alive) detailError.value[id] = reason instanceof ApiError ? reason.detail : '发言失败'
  } finally {
    if (alive) { speakingBusy.value[id] = false; speakingFor.value = null; beginPolling() }
  }
}

function digestText(digest: unknown): string {
  if (digest == null) return '—'
  if (typeof digest === 'string') return digest
  if (Array.isArray(digest)) return digest.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))).join('；')
  return JSON.stringify(digest, null, 2)
}

onUnmounted(() => { alive = false; stopPolling(); history.dispose() })

onMounted(async () => {
  await Promise.allSettled([
    loadProfiles().then(() => { if (alive) return loadOrder() }).catch(() => { if (alive) hint.value = '模型配置加载失败，可重试；历史仍可浏览' }),
    loadSessions(),
  ])
  if (!alive) return
  beginPolling() // Initial progress AND selected detail are fetched immediately, not after an interval.
})

</script>

<template>
  <header class="page-head">
    <div>
      <h1>多模型讨论验证</h1>
      <p class="lede">系统先做本地证据 + 联网核对，再由多个模型顺序发言，最后一位主席估算下一交易日收盘上涨概率。</p>
    </div>
    <button class="btn btn-ghost" type="button" :disabled="loading" @click="refresh">刷新</button>
    <button class="btn btn-ghost" type="button" @click="reconcileStale">终止遗留会话</button>
  </header>
  <CouncilSourcesPanel />

  <p class="hint">这是讨论会估计，不计入综合评分，不是涨停概率，不是买入建议。概率指下一交易日收盘相对评估日收盘上涨的主观估计，未经统计校准。单位不明或关键证据不足时不展示数字。</p>

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
      <span v-if="progress.elapsed_seconds != null"> · 已耗时 {{ Math.round(progress.elapsed_seconds) }} 秒</span>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>讨论记录</h2><span class="muted">{{ sessions.length }} 场</span></div>
    <div v-if="loading && !sessions.length" class="skeleton" />
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
            <td class="right"><b>{{ probabilityText(row) }}</b></td>
            <td>{{ confidenceLabel(row.conclusion?.confidence) }}</td>
            <td>{{ statusText(row) }}</td>
            <td class="muted">{{ displayTime(row.created_at) }}{{ row.timezone ? `（${row.timezone}）` : '' }}</td>
            <td class="right"><button class="btn btn-ghost" type="button" @click.stop="open(row)" @keydown.enter.stop>{{ expanded === row.id ? '收起' : '详情' }}</button>
              <button class="btn btn-ghost" type="button" :disabled="deleting[row.id] || row.status === 'running' || speakingBusy[row.id]" :title="row.status === 'running' ? '运行中的讨论不能删除' : '永久删除本场讨论'" @click.stop="removeSession(row)" @keydown.enter.stop>{{ deleting[row.id] ? '删除中…' : '删除' }}</button>
              <span v-if="deleteError[row.id]" class="alert" role="alert">{{ deleteError[row.id] }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-for="row in sessions.filter((item) => expanded === item.id)" :key="'detail-' + row.id" class="council-detail">
      <p v-if="detailLoading[row.id]" class="hint" role="status">正在加载最新对话…</p>
      <p v-if="detailError[row.id]" class="alert" role="alert">{{ detailError[row.id] }} <button class="btn btn-ghost" @click="retryDetail(row.id)">重试加载</button></p>
      <h3>{{ row.stock_name }} {{ row.stock_code }} · {{ row.eval_date }}</h3>
      <div class="muted">评估时间：{{ displayTime(row.created_at) }}（{{ row.timezone || 'Asia/Shanghai' }}）<template v-if="durationText(row)"> · 耗时：{{ durationText(row) }}</template></div>
      <div v-if="row.error && opening !== row.id" class="alert">{{ row.error }}</div>
      <template v-if="row.conclusion && readyFor(row)">
        <p class="hint">下一交易日收盘上涨估计：<b>{{ probabilityText(row) }}</b>，信心：{{ confidenceLabel(row.conclusion.confidence) }}。{{ row.conclusion.disclaimer }}</p>
        <p class="hint">{{ row.conclusion.probability_notice || '模型主观估计，未经统计校准，不是实测上涨频率。' }}</p>
        <p v-if="row.conclusion.probability_reason" class="alert">{{ row.conclusion.probability_reason }}</p>
        <details class="council-conclusion"><summary>主持人结论与依据（展开）</summary>
        <details v-if="row.conclusion.probability_audit"><summary>概率单位 / 原始值与规范化依据（不擅改历史值）</summary><pre>{{ JSON.stringify(row.conclusion.probability_audit, null, 2) }}</pre></details>
        <p class="hint">综述：{{ row.conclusion.summary || '—' }}</p>
        <div class="detail-grid">
          <div><b>支持上涨</b><p>{{ joinList(row.conclusion.bullish) }}</p></div>
          <div><b>不支持上涨</b><p>{{ joinList(row.conclusion.bearish) }}</p></div>
          <div><b>共识</b><p>{{ joinList(row.conclusion.agreements) }}</p></div>
          <div><b>分歧</b><p>{{ joinList(row.conclusion.disagreements) }}</p></div>
          <div><b>风险</b><p>{{ joinList(row.conclusion.risks) }}</p></div>
          <div><b>CNE6 因子</b><p>{{ joinList(row.conclusion.cne6_notes) }}</p></div>
        </div>
        </details>
      </template>

      <section class="group-chat" aria-label="多模型群聊" :aria-busy="opening === row.id">
        <div class="group-chat-head"><b>{{ row.stock_name }} · 讨论群</b><span>{{ row.participants.length }} 位模型 · 北京时间</span></div>
        <div class="chat-current" aria-live="off">{{ opening===row.id ? '读取最新对话' : statusText(row) }}<template v-if="row.status==='running' && opening!==row.id"> · {{ currentStage(row).label }}<span v-if="currentStage(row).seconds != null"> · 本阶段 {{ currentStage(row).seconds }} 秒</span></template><span v-if="row.duration_seconds != null"> · 本场累计 {{ durationText(row) }}</span></div>
        <div class="chat-viewport" :ref="bindChatViewport" @scroll="trackScroll">
          <p class="chat-notice">展示真实处理状态与可公开分析、依据和疑点，不展示或伪造模型内部思考链。</p>
          <p v-if="opening === row.id" class="chat-empty" role="status">正在读取本场最新对话…</p>
          <p v-else-if="!readyFor(row)" class="chat-empty">{{ detailError[row.id] ? '最新详情未加载，未展示列表中的旧对话；请点击重试。' : '等待最新详情…' }}</p>
          <p v-else-if="!transcriptItems(row).length" class="chat-empty">{{ emptyConversation(row) }}</p>
          <template v-for="message in (readyFor(row) ? chatMessages(row) : [])" :key="message.key">
            <div v-if="message.kind === 'system'" class="chat-system" :data-message-key="message.key">{{ message.content }} <small>{{ displayTime(message.time) }}</small></div>
            <article v-else class="chat-message" :data-message-key="message.key" :class="{ mine: message.kind === 'user', chair: message.kind === 'chairman' }">
              <div class="chat-avatar" aria-hidden="true">{{ message.kind === 'user' ? '我' : message.name.slice(0, 2) }}</div>
              <div class="chat-message-body"><div class="chat-name">{{ message.name }} <span v-if="message.kind === 'chairman'">· 主持人</span> <small>{{ displayTime(message.time) }}</small></div>
                <div class="chat-bubble" :class="{ 'chat-error': message.error }"><CouncilMessageContent :text="message.error || (message.kind==='user' ? message.content : publicContent(message.content)) || '未返回可公开的分析文本'" /></div>
                <details v-if="message.model || message.elapsed != null" class="chat-meta"><summary>模型 / 耗时</summary>{{ message.model }} · {{ message.elapsed == null ? '未记录耗时' : `${(message.elapsed / 1000).toFixed(1)} 秒` }}</details>
              </div>
            </article>
          </template>
        </div>
        <div v-if="!following && readyFor(row)" class="chat-jump"><button class="btn btn-ghost" @click="goBottom">{{ unread ? `${unread} 条新消息 · ` : '' }}回到底部</button></div>
        <div class="chat-composer">
          <div class="chat-controls"><label>点名 <select v-model="selectedSpeaker[row.id]" aria-label="选择发言模型"><option disabled value="">选择模型</option><option v-for="p in keyProfiles" :key="p.id" :value="p.id">{{ p.name || p.model }}</option></select></label>
            <button class="btn btn-ghost" :disabled="!readyFor(row) || busyFor(row) || !selectedSpeaker[row.id]" @click="askSpeaker(row.id)">{{ speakingFor === row.id ? '处理中…' : '请该模型发言 / 续轮' }}</button>
            <button class="btn btn-ghost" :disabled="!readyFor(row) || busyFor(row)" @click="askChair(row)">请主持人总结</button></div>
          <label class="sr-only" :for="`opinion-${row.id}`">我的意见</label>
          <textarea :id="`opinion-${row.id}`" v-model="userInput[draftKey(row)]" maxlength="4000" rows="3" placeholder="说说你的意见、补充证据或疑问…" @compositionstart="composing[draftKey(row)] = true" @compositionend="endComposition(row)" @keydown="composerKey($event,row)" />
          <p v-if="row.status==='running' || progress?.running" class="chat-notice">{{ row.status==='running' || progress?.session_id===row.id ? '本场模型' : '其他会话' }}正在处理，当前只能保留草稿；完成后可发送意见。</p>
          <p class="composer-help">{{ !readyFor(row) ? '详情加载后可发送，当前输入只保留本场草稿' : busyFor(row) ? '处理中 Enter 换行，草稿可继续编辑' : 'Enter 发送 · Shift+Enter 换行 · 中文选词不发送' }}<span v-if="speakingBusy[row.id] && speakingFor!==row.id"> · 意见正在保存，请勿重复发送</span></p>
          <div class="chat-send-row"><span>{{ (userInput[draftKey(row)] || '').length }}/4000 · 意见保存后，后续模型与主持人都能看到</span><button class="btn" :disabled="!readyFor(row) || busyFor(row) || !(userInput[draftKey(row)] || '').trim()" @click="addUserMessage(row.id)">{{ speakingBusy[row.id] && speakingFor!==row.id ? '保存中…' : '发送意见' }}</button></div>
          <p v-if="detailError[row.id]" class="alert" role="alert">{{ detailError[row.id] }}</p><p v-if="detailHint[row.id]" class="hint">{{ detailHint[row.id] }}</p>
        </div>
      </section>

      <details class="council-evidence" :open="!!evidenceOpen[draftKey(row)]" @toggle="evidenceOpen[draftKey(row)] = ($event.currentTarget as HTMLDetailsElement).open"><summary>证据摘要与来源（默认折叠）</summary>
      <template v-if="evidenceOpen[draftKey(row)]">
      <h4>证据摘要</h4>
      <div v-if="row.evidence.quality" class="hint">实际取到 {{ row.evidence.quality.successful_source_count }}/{{ row.evidence.quality.source_count }} 个来源；有效空记录 {{ row.evidence.quality.source_status_counts.empty || 0 }}；接口故障 {{ row.evidence.quality.source_status_counts.failed || 0 }}。不是缺失补零，也不代表估计已经校准。<p v-for="missing in row.evidence.quality.critical_missing" :key="missing">缺项：{{ missing }}</p></div>
      <p v-if="evidenceSummary(row.evidence)" class="hint">{{ evidenceSummary(row.evidence) }}</p>
      <div v-if="chartBars(row.evidence).length" class="chart-box">
        <div class="chart-head">
          <span>近 30 个交易日 K 线（合并后）</span>
          <span class="muted">{{ row.evidence.kline?.chart?.source || '本地数据' }} · {{ chartBars(row.evidence).length }} 根</span>
        </div>
        <KlineChart :bars="chartBars(row.evidence) as any" :height="340" />
      </div>
      <div v-else class="hint">没有可展示的 K 线数值序列。</div>

      <div v-for="[category, items] in groupedWeb(row.evidence)" :key="category" class="evidence-group">
        <h5>{{ categoryLabel(category) }} <span class="muted">{{ items.length }} 采集项</span></h5>
        <div v-for="(item, itemIndex) in items" :key="itemIndex" class="evidence-item">
          <div class="evidence-item-head">
            <a v-if="/^https?:\/\//i.test(item.url || '')" :href="item.url" target="_blank" rel="noopener noreferrer">{{ item.source }}</a><b v-else>{{ item.source }}</b>
            <span class="status" :class="item.status">{{ statusLabel(item.status) }}</span>
            <span class="muted">{{ displayTime(item.fetched_at) }}</span>
          </div>
          <div v-if="item.registry" class="muted">清单 {{ item.registry.version }} · 解析器 {{ item.registry.parser_version }} · 渠道 {{ item.registry.provider }} <span v-if="item.cache?.hit">· 缓存命中（{{ item.cache.age_seconds }}秒前，保留原抓取时间）</span></div>
          <div v-if="item.window?.kind === 'latest_disclosed_report'" class="muted">截至 {{ item.window.as_of }} 最新已披露财报，不限近30天；报告期与披露日见摘要。</div>
          <div v-else-if="item.window?.kind === 'trading_days'" class="muted">近约 {{ item.window.requested_trading_days || 30 }} 个交易日 · 截至 {{ item.window.end }}</div>
          <div v-else-if="item.window" class="muted">观察区间：{{ item.window.start }} 至 {{ item.window.end }}（含首尾 {{ item.window.days }} 天）</div>
          <div v-if="item.observed_interval" class="muted">实际返回：{{ item.observed_interval.start }} 至 {{ item.observed_interval.end }} · {{ item.observed_interval.count }} 条</div>
          <details v-if="item.http_attempts?.length"><summary>请求诊断 / 有界重试 · {{ item.elapsed_seconds }}秒</summary><div v-for="(attempt, attemptIndex) in item.http_attempts" :key="attemptIndex" class="muted">第{{ attempt.attempt }}次：HTTP {{ attempt.status_code || '未收到响应' }} · {{ attempt.content_type || '无Content-Type' }} · {{ attempt.error || '' }}</div></details>
          <p v-if="item.summary" class="evidence-summary">{{ item.summary }}</p>
          <details>
            <summary>来源原文</summary>
            <pre class="evidence">{{ digestText(item.digest) }}</pre>
          </details>
        </div>
      </div>

      <details>
        <summary>完整证据 JSON（默认折叠）</summary>
        <pre class="evidence">{{ JSON.stringify(row.evidence, null, 2) }}</pre>
      </details>
      </template>
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
.conversation-tools { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 8px 0; }
.conversation-tools .field { min-width: 220px; }
.transcript-item { padding: 8px 0; border-bottom: 1px solid rgba(127,127,127,0.18); }
.transcript-item.user { background: rgba(232,184,109,0.06); padding: 8px; border-radius: 4px; }
.transcript-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.evidence-summary { font-size: 13px; white-space: pre-wrap; margin: 4px 0 0; }
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

.group-chat { border:1px solid rgba(127,127,127,.25); border-radius:12px; overflow:hidden; margin:18px 0; background:var(--bg,#f1f3f2); }
.group-chat-head { display:flex; justify-content:space-between; gap:12px; padding:16px; border-bottom:1px solid rgba(127,127,127,.2); }
.group-chat-head span,.chat-name small,.chat-meta,.chat-send-row span { font-size:12px; opacity:.7; }
.chat-viewport { height:min(58vh,620px); min-height:280px; overflow:auto; padding:18px; overscroll-behavior:contain; }
.chat-notice,.chat-system { text-align:center; font-size:12px; opacity:.75; margin:14px auto; max-width:86%; }
.chat-system { background:rgba(127,127,127,.12); border-radius:8px; padding:7px 12px; width:fit-content; }.chat-system small { display:block; font-size:10px; opacity:.7; margin-top:3px; }
.chat-message { display:flex; gap:10px; margin:20px 0; align-items:flex-start; }.chat-message.mine { flex-direction:row-reverse; }
.chat-avatar { flex:0 0 36px; width:36px; height:36px; border-radius:10px; display:grid; place-items:center; background:#3f6d9d; color:white; font-size:12px; }.mine .chat-avatar { background:#398455; }.chair .chat-avatar { background:#946f28; }
.chat-message-body { max-width:82%; min-width:0; }.mine .chat-message-body { text-align:right; }.chat-name { font-size:12px; margin-bottom:6px; opacity:.8; }.chat-name small { margin-left:8px; }
.chat-bubble { white-space:normal; padding:12px 15px; border-radius:3px 12px 12px; background:rgba(127,127,127,.10); overflow-wrap:anywhere; line-height:1.7; text-align:left; }.mine .chat-bubble { background:#c4eccd; color:#193b22; border-radius:12px 3px 12px 12px; }.chat-error { border:1px solid #b85050; }.chat-meta { margin-top:6px; text-align:left; }
.chat-composer { position:relative; background:var(--bg,#fff); padding:14px; border-top:1px solid rgba(127,127,127,.2); }.chat-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }.chat-controls select { max-width:220px; }
.chat-composer textarea { width:100%; resize:vertical; box-sizing:border-box; border:1px solid rgba(127,127,127,.35); border-radius:7px; background:transparent; color:inherit; padding:10px; font:inherit; }.chat-send-row { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:8px; }.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
@media(max-width:640px) { .chat-viewport { padding:10px; }.chat-message-body { max-width:85%; }.group-chat-head { flex-direction:column; }.chat-send-row span { max-width:65%; } }
.chat-current{padding:8px 16px;font-size:12px;border-bottom:1px solid #89939c33;overflow-wrap:anywhere}.chat-empty{padding:28px 12px;text-align:center;opacity:.7}.chat-jump{text-align:center;padding:4px;background:var(--bg,#fff)}.composer-help{font-size:11px;opacity:.7;margin:6px 0}.council-evidence,.council-conclusion{margin:12px 0}.council-evidence>summary,.council-conclusion>summary{cursor:pointer;font-size:13px}.chat-composer textarea{min-height:70px;max-height:180px}.chat-viewport{overflow-anchor:auto}
@media(max-width:640px){.chat-viewport{height:44vh;min-height:220px}.chat-composer{padding:10px}.chat-controls select{max-width:100%}.chat-controls label{width:100%}.chat-send-row{align-items:flex-end}.chat-name small{display:block;margin-left:0}}
</style>
