import { api, ApiError } from '@/api/client'
import type { CaptureStatus, ManualResult } from '@/api/types'
import { lastSaved, saveExportedText } from '@/utils/saveFile'
import { reactive } from 'vue'

/** 手动采集的共享状态：总览和个股相似度两张卡片共用，切换页面不丢；刷新 / 重启后从后端读回最近一次快照。 */
export const state = reactive({
  status: null as CaptureStatus | null,
  result: null as ManualResult | null,
  error: '',
  fallbackHint: '',
  busy: false,
  note: '',
  warming: false,
  exporting: false,
  ranking: false,
  loaded: false,
  savedPath: null as string | null,
})

let timer: number | undefined
let warmTimer: number | undefined

function stopPoll() {
  if (timer) window.clearInterval(timer)
  timer = undefined
}

function startPoll() {
  if (timer) return // 已在轮询，不重复
  timer = window.setInterval(poll, 800)
}

async function poll() {
  try {
    const s = await api.captureStatus()
    state.status = s
    if (!s.running) {
      stopPoll()
      state.busy = false
      if (s.phase === 'done') await loadLatest()
      else if (s.phase === 'error') state.error = s.message
    }
  } catch (e) {
    stopPoll()
    state.busy = false
    state.error = e instanceof Error ? e.message : String(e)
  }
}

export async function loadLatest(date?: string) {
  try {
    state.result = await api.manualSnapshot(date)
  } catch {
    // 还没采集过
  }
}

/** 每次挂载都调用：首次读回快照；后台若还在采集则接上进度（不重新开始）。 */
export async function initCapture() {
  if (!state.loaded) {
    state.loaded = true
    await loadLatest()
  }
  const s = await api.captureStatus().catch(() => null)
  if (s?.running) {
    state.status = s
    state.busy = true
    startPoll()
  }
}

export function startWarmWatch() {
  if (warmTimer) return
  const check = () =>
    api
      .backtestWarm()
      .then((w) => {
        state.warming = w.running
        warmTimer = w.running ? window.setTimeout(check, 3000) : undefined
      })
      .catch(() => {
        warmTimer = undefined
      })
  warmTimer = window.setTimeout(check, 0)
}

export async function capture(fallback = false) {
  if (state.busy) return
  state.error = ''
  state.fallbackHint = ''
  state.note = ''
  state.busy = true
  try {
    const r = await api.startCapture(fallback)
    if ('result' in r) {
      state.result = r
      state.busy = false
      return
    }
    state.status = r
    startPoll()
  } catch (e) {
    state.busy = false
    if (e instanceof ApiError && e.status === 409) state.fallbackHint = e.message
    else state.error = e instanceof Error ? e.message : String(e)
  }
}

export async function cancel() {
  await api.cancelCapture().catch(() => undefined)
}

export async function exportInput(format: 'txt' | 'md' = 'txt') {
  if (state.exporting || !state.result) return
  state.exporting = true
  state.error = ''
  try {
    const daily = state.result.kind === 'daily'
    const file = daily ? await api.aiInput(format, state.result.date) : await api.aiInput(format, undefined, 'manual')
    const mode = await saveExportedText(file.filename, file.text, file.blob)
    state.savedPath = lastSaved.path
    state.note = mode === 'cancel' ? '已取消保存' : `已${mode === 'dialog' ? '保存到 ' + (lastSaved.path ?? file.filename) : '下载 ' + file.filename}（${file.text.length} 字），发给外部 AI 分析`
  } catch (e) {
    state.error = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
  } finally {
    state.exporting = false
  }
}

export async function rank() {
  if (state.ranking || !state.result) return
  state.ranking = true
  state.note = '正在生成 AI 榜单…'
  try {
    if (state.result.kind === 'daily') await api.generateAiRanking({ date: state.result.date })
    else await api.generateAiRanking({ snapshot: 'manual' })
    state.note = 'AI 榜单已生成，到「AI 榜单」页查看'
  } catch (e) {
    state.note = ''
    state.error = e instanceof Error ? e.message : String(e)
  } finally {
    state.ranking = false
  }
}
