import { api, ApiError } from '@/api/client'
import type {
  DailyRankHistoryResponse,
  DailyRankPrepareAck,
  DailyRankRunnerStatus,
  DailyRankState,
  PicksCheckResponse,
  PicksSummary,
} from '@/api/types'
import { reactive } from 'vue'

/**
 * 每日 AI 固定榜的读数：两段账分开，一条一分钱不花、一条按 token 付钱。
 *
 * 跑排名的时候轮询打的是 `/ai/daily-rank/status`——它不碰库，只回答「在不在跑、
 * 收到多少字」。跑完那一拍才回头读 `/ai/daily-rank`，那一份里的 `board` 与 `runner`
 * 是后端在同一把锁里配好的一对，前端整个存下来、不拆成两次赋值。
 * 读不到就维持上一份：花钱排好的名次不该因为一次网络抖就消失。
 */

const RUNNING_TICK_MS = 2000
const IDLE_TICK_MS = 30000

export const dailyRank = reactive({
  state: null as DailyRankState | null,
  runner: null as DailyRankRunnerStatus | null,
  history: null as DailyRankHistoryResponse | null,
  summary: null as PicksSummary | null,
  lastCheck: null as PicksCheckResponse['reconcile'] | null,
  preparing: false,
  ranking: false,
  checking: false,
  /** 起了线程、还没读到跑完那一份。 */
  pendingRank: false,
  /** 一次都没读到过——只有这时候才该红。 */
  error: '',
  /** 这一拍没读到，屏幕上还是上一份。 */
  note: '',
  actionError: '',
})

let refs = 0
let timer = 0
let ticks = 0
let inFlight = false

function detail(reason: unknown, fallback: string): string {
  return reason instanceof ApiError ? reason.detail : fallback
}

function isRunning(): boolean {
  if (dailyRank.runner?.running) return true
  return dailyRank.state?.runner?.running === true
}

function tickMs(): number {
  return isRunning() || dailyRank.pendingRank || dailyRank.ranking ? RUNNING_TICK_MS : IDLE_TICK_MS
}

async function loadState(): Promise<void> {
  try {
    dailyRank.state = await api.dailyRank()
    dailyRank.runner = dailyRank.state.runner
    dailyRank.error = ''
    dailyRank.note = ''
  } catch (reason) {
    const message = detail(reason, '固定榜读取失败')
    if (dailyRank.state) dailyRank.note = `${message}，屏幕上还是上一份`
    else dailyRank.error = message
  }
}

async function loadRunner(): Promise<void> {
  try {
    const status = await api.dailyRankStatus()
    const wasRunning = isRunning()
    dailyRank.runner = status
    if (status.running) {
      dailyRank.pendingRank = true
    } else if (wasRunning || dailyRank.pendingRank) {
      dailyRank.pendingRank = false
      // 跑完这一趟改了三份读数：当天那一份、备过的日子那张表、对账账本（推荐刚落库）。
      // 只回头读当天的那一份，另两张就会一直显示上一趟的样子，直到重进页面才追上。
      await Promise.all([loadState(), loadHistory(), loadSummary()])
    }
  } catch {
    dailyRank.pendingRank = false
  }
}

async function loadHistory(): Promise<void> {
  try {
    dailyRank.history = await api.dailyRankHistory(15)
  } catch {
    // 历史读不到不影响当天那一份
  }
}

async function loadSummary(): Promise<void> {
  try {
    dailyRank.summary = await api.picksSummary()
  } catch {
    // 账本读不到就维持上一份
  }
}

async function tick(): Promise<void> {
  if (inFlight) return
  inFlight = true
  ticks += 1
  try {
    if (isRunning() || dailyRank.pendingRank) {
      await loadRunner()
    } else if (refs > 0) {
      if (ticks === 1) await Promise.all([loadState(), loadHistory(), loadSummary()])
      else await loadState()
    }
  } finally {
    inFlight = false
    if (!refs && !dailyRank.pendingRank && !isRunning()) {
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

export function retainDailyRank(): void {
  refs += 1
  schedule()
}

export function releaseDailyRank(): void {
  refs = Math.max(0, refs - 1)
  if (!refs && !dailyRank.pendingRank && !isRunning() && timer) {
    window.clearTimeout(timer)
    timer = 0
  }
}

/** 回到前台那一拍补一次读数，不等下一次定时。 */
export function refreshDailyRankNow(): void {
  ticks = 0
  void loadState()
  void loadHistory()
  void loadSummary()
  if (timer) window.clearTimeout(timer)
  timer = 0
  schedule()
}

export async function prepareFeed(): Promise<void> {
  if (dailyRank.preparing) return
  dailyRank.preparing = true
  dailyRank.actionError = ''
  try {
    const ack: DailyRankPrepareAck = await api.dailyRankPrepare()
    if (!ack.ok) dailyRank.actionError = ack.error || '备输入没有落库'
    await loadState()
    await loadHistory()
  } catch (reason) {
    dailyRank.actionError = detail(reason, '备好输入失败')
  } finally {
    dailyRank.preparing = false
  }
}

export async function startRanking(): Promise<void> {
  if (dailyRank.ranking || dailyRank.pendingRank) return
  dailyRank.ranking = true
  dailyRank.actionError = ''
  const day = dailyRank.state?.board?.trade_date || undefined
  try {
    await api.dailyRankRank(day)
    dailyRank.pendingRank = true
    dailyRank.runner = { running: true, trade_date: day }
  } catch (reason) {
    dailyRank.actionError = detail(reason, '排名启动失败')
  } finally {
    dailyRank.ranking = false
  }
  if (timer) window.clearTimeout(timer)
  // 归零是必须的：`schedule()` 认的是「已经排好了」这个变量，
  // 只 clearTimeout 不归零就会在这里把整条轮询链断掉——进度条会永远停在「排名中」。
  timer = 0
  schedule()
}

export async function runPicksCheck(): Promise<void> {
  if (dailyRank.checking) return
  dailyRank.checking = true
  dailyRank.actionError = ''
  try {
    const result = await api.picksCheck()
    dailyRank.summary = result
    dailyRank.lastCheck = result.reconcile
  } catch (reason) {
    dailyRank.actionError = detail(reason, '对账失败')
  } finally {
    dailyRank.checking = false
  }
}
