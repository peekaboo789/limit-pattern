import { api } from '@/api/client'
import type { MonitorEvent } from '@/api/types'
import { reactive } from 'vue'

export interface Toast {
  id: number
  level: 'warn' | 'alert'
  title: string
  text: string
}

export const monitorState = reactive({
  lastId: 0,
  toasts: [] as Toast[],
  sound: localStorage.getItem('lp-monitor-sound') !== '0',
  started: false,
})

const seen = new Set<string>()
let timer: number | undefined

export function setSound(on: boolean) {
  monitorState.sound = on
  localStorage.setItem('lp-monitor-sound', on ? '1' : '0')
}

function beep(level: 'warn' | 'alert') {
  if (!monitorState.sound) return
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctx) return
    const ctx = new Ctx()
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.frequency.value = level === 'alert' ? 880 : 660
    gain.gain.value = 0.08
    osc.connect(gain)
    gain.connect(ctx.destination)
    osc.start()
    osc.stop(ctx.currentTime + (level === 'alert' ? 0.6 : 0.25))
  } catch {
    // 没有声音权限就算了
  }
}

export function dismiss(id: number) {
  monitorState.toasts = monitorState.toasts.filter((toast) => toast.id !== id)
}

function show(date: string, event: MonitorEvent) {
  const key = `${date}:${event.code}:${event.level}`
  if (seen.has(key)) return
  seen.add(key)
  const pct = event.pct == null ? '' : `${event.pct >= 0 ? '+' : ''}${event.pct.toFixed(2)}%`
  const title = event.level === 'alert' ? '涨幅超 6% 报警' : '涨幅超 3% 提醒'
  const text = `${event.code} ${event.name} ${pct}${event.rank ? `（昨日第 ${event.rank} 名）` : ''}`
  monitorState.toasts = [...monitorState.toasts, { id: event.id, level: event.level, title, text }].slice(-6)
  beep(event.level)
  const notify = window.pywebview?.api?.notify
  if (typeof notify === 'function') void Promise.resolve(notify(title, text, key)).catch(() => undefined)
  window.setTimeout(() => dismiss(event.id), event.level === 'alert' ? 20000 : 10000)
}

async function poll() {
  try {
    const payload = await api.monitorEvents(monitorState.lastId)
    for (const event of payload.items) show(payload.date, event)
    monitorState.lastId = Math.max(monitorState.lastId, payload.last_id)
  } catch {
    // 后端暂时不通，下一轮再试
  }
}

export function startMonitorEvents() {
  if (monitorState.started || import.meta.env.VITE_USE_MOCK === '1') return
  monitorState.started = true
  void poll()
  timer = window.setInterval(poll, 5000)
}

export function stopMonitorEvents() {
  if (timer !== undefined) window.clearInterval(timer)
  monitorState.started = false
}
