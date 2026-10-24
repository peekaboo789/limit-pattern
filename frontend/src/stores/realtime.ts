import { api, ApiError } from '@/api/client'
import type { BoardState, RealtimeStatus, RealtimeTop, SingleState } from '@/api/types'
import { reactive } from 'vue'

/**
 * 盘中监控的读数只留这一份：页面切走再回来、AI 那条链路跑完，都读这里。
 *
 * 三件事必须说在前头：
 * * **单飞闸门**（`inFlight`）：上一拍还没回来就不发下一拍，否则慢的时候请求会在
 *   后端排队，页面拿到的是过一拍的价格却以为是刚取的。
 * * **读不到维持上一份**：网络抖一下不该把已经看见的榜抹掉，更不该顶一条红字。
 *   只有「一次都没读到过」才配得上 `.alert`。
 * * **AI 的进度与结论是一起读的**：后端每次返回 `{runner, report}` 一对，
 *   这里也整个存下来，绝不把「在不在跑」和「跑完的那一份」拆成两次赋值——
 *   拆开了就会在两次赋值之间露出「没在跑、也没有结论」那一瞬。
 * * **进页面先补读已有的那一份分析**：跑完的分析存在后端（内存 + 存档），
 *   只等「有链路在跑」才去读就会出现「程序重启/换页回来就什么都没」——那是要重进页才看得见，
 *   属于读数没只留一份。所以每次挂载先读一拍，之后只在有在跑的链路时才跟着读。
 */

const MIN_TICK_MS = 1200
const MAX_TICK_MS = 10000
const STATUS_EVERY_TICKS = 5

function emptyBoard(): BoardState {
  return { runner: { running: false }, source: 'none', report: null }
}

export const realtime = reactive({
  top: null as RealtimeTop | null,
  status: null as RealtimeStatus | null,
  metric: 'score',
  topN: 50,
  /** 这一拍没读到，但屏幕上还是上一份。 */
  note: '',
  /** 一次都没读到过——只有这时候才该红。 */
  error: '',
  board: emptyBoard() as BoardState,
  /** 点了「分析这一榜」之后必须读到一份结论，不管跑没跑完。 */
  boardPending: false,
  startingBoard: false,
  single: null as SingleState | null,
  singleCode: '',
  singlePending: false,
  startingSingle: false,
  actionError: '',
})

let refs = 0
let timer = 0
let ticks = 0
let inFlight = false
/** 进页面那一拍要不要补读一次「已有的那一份分析」。每次重新挂载都重置。 */
let entryRead = false

function detail(reason: unknown, fallback: string): string {
  return reason instanceof ApiError ? reason.detail : fallback
}

function tickMs(): number {
  const seconds = realtime.top?.refresh_seconds ?? realtime.status?.refresh_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return 2000
  return Math.min(MAX_TICK_MS, Math.max(MIN_TICK_MS, Math.round(seconds * 1000)))
}

function statusWord(value: string): string {
  if (value === 'warming') return '预热中'
  if (value === 'unavailable') return '取不到'
  return value
}

async function pollBoard(): Promise<void> {
  try {
    const top = await api.realtimeTop({ metric: realtime.metric, top_n: realtime.topN })
    realtime.top = top
    realtime.error = ''
    if (top.status === 'live') {
      realtime.note = top.error ? `上一轮报错：${top.error}` : ''
    } else {
      realtime.note = `榜还没有可用的一份（${statusWord(top.status)}${top.error ? `：${top.error}` : ''}），屏幕上还是上一份`
    }
    if (top.snapshot?.top_n) realtime.topN = top.snapshot.top_n
    if (top.metric) realtime.metric = top.metric
  } catch (reason) {
    const message = detail(reason, '监控榜读取失败')
    if (realtime.top) realtime.note = `${message}，屏幕上还是上一份`
    else realtime.error = message
  }
}

async function pollStatus(): Promise<void> {
  try {
    realtime.status = await api.realtimeStatus()
  } catch {
    // 状态卡读不到就沿用上一份：它只是说明节奏与窗口，不该影响榜本身
  }
}

async function pollAnalysis(): Promise<void> {
  try {
    const state = await api.boardAnalysis()
    realtime.board = state
    if (!state.runner.running) realtime.boardPending = false
  } catch (reason) {
    realtime.board = { ...realtime.board, runner: { running: false }, source: realtime.board.source }
    realtime.boardPending = false
    realtime.actionError = detail(reason, '综合分析进度读取失败')
  }
}

async function pollSingle(): Promise<void> {
  const code = realtime.singleCode
  if (!code) return
  try {
    const state = await api.singleAnalysis(code)
    realtime.single = state
    if (!state.runner.running) realtime.singlePending = false
  } catch (reason) {
    if (realtime.single) realtime.single = { ...realtime.single, runner: { running: false } }
    realtime.singlePending = false
    realtime.actionError = detail(reason, '直评进度读取失败')
  }
}

function aiInFlight(): boolean {
  return (
    realtime.startingBoard ||
    realtime.boardPending ||
    realtime.board.runner.running ||
    realtime.startingSingle ||
    realtime.singlePending ||
    realtime.single?.runner.running === true
  )
}

function shouldPoll(): boolean {
  return refs > 0 || aiInFlight()
}

async function tick(): Promise<void> {
  if (inFlight) return
  inFlight = true
  ticks += 1
  const entry = refs > 0 && !entryRead
  if (entry) entryRead = true
  try {
    if (refs > 0) {
      if (ticks === 1 || ticks % STATUS_EVERY_TICKS === 0 || !realtime.top) await pollStatus()
      await pollBoard()
    }
    if (entry || realtime.startingBoard || realtime.boardPending || realtime.board.runner.running) {
      await pollAnalysis()
    }
    if (
      (entry && realtime.singleCode) ||
      realtime.startingSingle ||
      realtime.singlePending ||
      realtime.single?.runner.running
    )
      await pollSingle()
  } finally {
    inFlight = false
    if (!shouldPoll()) {
      window.clearTimeout(timer)
      timer = 0
      return
    }
    timer = window.setTimeout(() => void tick(), tickMs())
  }
}

function schedule(): void {
  if (timer) return
  timer = window.setTimeout(() => void tick(), 0)
}

export function retainRealtime(): void {
  refs += 1
  if (refs === 1) entryRead = false
  schedule()
}

export function releaseRealtime(): void {
  refs = Math.max(0, refs - 1)
  if (!shouldPoll() && timer) {
    window.clearTimeout(timer)
    timer = 0
  }
}

/** 回到前台或手动刷新那一拍立刻补一次：读数不该等下一次定时才追上。 */
export function refreshRealtimeNow(withAnalysis = false): void {
  if (withAnalysis) entryRead = false
  if (!timer) {
    schedule()
    return
  }
  window.clearTimeout(timer)
  timer = window.setTimeout(() => void tick(), 0)
}

export function setRankMetric(metric: string): void {
  if (metric === realtime.metric) return
  realtime.metric = metric
  refreshRealtimeNow()
}

export function setTopCount(topN: number): void {
  const min = realtime.top?.top_n_min ?? 10
  const max = realtime.top?.top_n_max ?? 200
  const next = Math.min(max, Math.max(min, Math.round(topN)))
  if (next === realtime.topN) return
  realtime.topN = next
  refreshRealtimeNow()
}

export async function startBoardAnalysis(): Promise<void> {
  if (realtime.startingBoard) return
  realtime.startingBoard = true
  realtime.actionError = ''
  try {
    const ack = await api.startBoardAnalysis()
    realtime.boardPending = true
    if (!ack.accepted && !ack.already_running) realtime.actionError = '后端没有接这一份分析，稍后再试一次'
    refreshRealtimeNow()
  } catch (reason) {
    realtime.actionError = detail(reason, '综合分析启动失败')
  } finally {
    realtime.startingBoard = false
  }
}

export async function startSingleAnalysis(code: string): Promise<void> {
  if (!code || realtime.startingSingle) return
  realtime.startingSingle = true
  realtime.actionError = ''
  realtime.singleCode = code
  realtime.single = null
  try {
    await api.startSingleAnalysis(code)
    realtime.singlePending = true
    refreshRealtimeNow()
  } catch (reason) {
    realtime.actionError = detail(reason, '单股直评启动失败')
    realtime.singlePending = false
  } finally {
    realtime.startingSingle = false
  }
}

export function clearSingle(): void {
  realtime.single = null
  realtime.singleCode = ''
  realtime.singlePending = false
}
