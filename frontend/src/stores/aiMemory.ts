import { api, ApiError } from '@/api/client'
import type { AiMemoryDays, AiMemoryRunResult, AiMemorySummary } from '@/api/types'
import { dailyRank } from '@/stores/dailyRank'
import { reactive } from 'vue'

/**
 * 每日 JSON 快照（模型记忆 / 误判库）的读数。
 *
 * 「谁产生谁交值」：跑完那一拍直接用手上这份 run 结果填 `export` 与 `check`，
 * 不回头再问一次——屏幕上的数字因此和刚刚落盘的那两份文件必然出自同一次计算。
 * 读不到就维持上一份：一块临时写不进去的盘不该把已有的日子从页面上抹掉。
 */

export const aiMemory = reactive({
  summary: null as AiMemorySummary | null,
  days: null as AiMemoryDays | null,
  lastRun: null as AiMemoryRunResult | null,
  loading: false,
  running: false,
  /** 一次都没读到过——只有这时候才该红。 */
  error: '',
  /** 这一拍没读到，屏幕上还是上一份。 */
  note: '',
})

const IDLE_TICK_MS = 30000
let refs = 0
let timer = 0

function detail(reason: unknown, fallback: string): string {
  return reason instanceof ApiError ? reason.detail : fallback
}

export async function loadAiMemory(): Promise<void> {
  if (aiMemory.loading) return
  aiMemory.loading = true
  try {
    const summary = await api.aiMemorySummary()
    aiMemory.summary = summary
    aiMemory.days = summary.export
    aiMemory.error = ''
    aiMemory.note = ''
  } catch (reason) {
    const message = detail(reason, '快照读数加载失败')
    if (aiMemory.summary) aiMemory.note = `${message}，屏幕上还是上一份`
    else aiMemory.error = message
  } finally {
    aiMemory.loading = false
  }
}

/** 收盘后那条闭环由定时任务自己跑：页面开着就隔一阵读一次，落了新的一天不用重进页面。 */
export function retainAiMemory(): void {
  refs += 1
  void loadAiMemory()
  if (!timer) timer = window.setInterval(() => void loadAiMemory(), IDLE_TICK_MS)
}

export function releaseAiMemory(): void {
  refs = Math.max(0, refs - 1)
  if (!refs && timer) {
    window.clearInterval(timer)
    timer = 0
  }
}

/** 手动跑一遍收盘后那条闭环：对账 → 导当天 → 回填上一个交易日。一分钱都不花。 */
export async function runAiMemoryNow(): Promise<void> {
  if (aiMemory.running) return
  aiMemory.running = true
  try {
    const result = await api.runAiMemory()
    aiMemory.lastRun = result
    aiMemory.days = result.export
    // 这一趟顺手对了账：同页的「它的历史账」读的是同一本账，当场交给它，不等下一拍。
    dailyRank.summary = result.check
    aiMemory.summary = aiMemory.summary
      ? { ...aiMemory.summary, check: result.check, export: result.export, latest_quote_date: result.trade_date }
      : aiMemory.summary
    aiMemory.error = ''
    aiMemory.note = ''
    // 一次都没读到过摘要时上面那一句换不上去：回头读一份完整的。
    if (!aiMemory.summary) void loadAiMemory()
  } catch (reason) {
    const message = detail(reason, '对账与快照没有跑成')
    if (aiMemory.summary) aiMemory.note = `${message}，屏幕上还是上一份`
    else aiMemory.error = message
  } finally {
    aiMemory.running = false
  }
}
