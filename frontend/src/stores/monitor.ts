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
  alertsActive: true,
  statusText: '',
  demoAlert: false,
  testResult: '',
})

// 按天持久化已提醒的键，重启后不重复弹
const SEEN_KEY = 'lp-monitor-seen'
function loadSeen(): { date: string; keys: string[] } {
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY) || '') as { date: string; keys: string[] }
  } catch {
    return { date: '', keys: [] }
  }
}
const stored = loadSeen()
const seen = new Set<string>(stored.keys ?? [])
let seenDate = stored.date ?? ''
function persistSeen(date: string) {
  if (date.startsWith('test-')) return
  if (date !== seenDate) {
    for (const key of [...seen]) if (!key.startsWith(`${date}:`)) seen.delete(key)
    seenDate = date
  }
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify({ date, keys: [...seen].filter((key) => key.startsWith(`${date}:`)) }))
  } catch {
    // 存不了就算了
  }
}
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

/** 网页版（Docker）：页面开着时用浏览器 Notification API；桌面版走 pywebview。 */
export function browserNotify(title: string, body: string, tag: string) {
  if (typeof window === 'undefined' || !('Notification' in window)) return
  try {
    if (Notification.permission === 'granted') new Notification(title, { body, tag })
  } catch {
    /* 某些浏览器只允许在 Service Worker 里弹通知，忽略 */
  }
}

export async function askBrowserNotify(): Promise<string> {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported'
  if (Notification.permission === 'default') return await Notification.requestPermission()
  return Notification.permission
}

export function show(date: string, event: MonitorEvent, titlePrefix = '') {
  const key = `${titlePrefix ? 'wl:' : ''}${date}:${event.code}:${event.level}`
  if (seen.has(key)) return
  seen.add(key)
  persistSeen(date)
  const pct = event.pct == null ? '' : `${event.pct >= 0 ? '+' : ''}${event.pct.toFixed(2)}%`
  const title = titlePrefix + (event.level === 'alert' ? '涨幅超 6% 报警' : '涨幅超 3% 提醒')
  const text = `${event.code} ${event.name} ${pct}${event.rank ? `（昨日第 ${event.rank} 名）` : ''}`
  monitorState.toasts = [...monitorState.toasts, { id: event.id, level: event.level, title, text }].slice(-6)
  beep(event.level)
  const notify = window.pywebview?.api?.notify
  if (typeof notify === 'function') void Promise.resolve(notify(title, text, key)).catch(() => undefined)
  else browserNotify(title, text, key)
  window.setTimeout(() => dismiss(event.id), event.level === 'alert' ? 20000 : 10000)
}

const CHANNEL: Record<string, string> = {
  toast: 'Windows 系统通知',
  balloon: '托盘气泡',
  msgbox: '置顶消息框',
  log: '只记了日志（系统通知、托盘、消息框都没成功）',
}

/** 「测试提醒」：弹一条 ≥3% 和一条 ≥6% 示例，红框演示 10 秒，并走一遍桌面通知兜底链。 */
export async function testAlerts() {
  const stamp = Date.now()
  const day = `test-${stamp}`
  show(day, { id: -stamp, code: '600000', name: '示例股票', level: 'warn', pct: 3.1, rank: 1 } as MonitorEvent)
  window.setTimeout(() => {
    show(day, { id: -stamp - 1, code: '600000', name: '示例股票', level: 'alert', pct: 6.2, rank: 1 } as MonitorEvent)
    monitorState.demoAlert = true
    window.setTimeout(() => (monitorState.demoAlert = false), 10000)
  }, 1500)
  const tester = window.pywebview?.api?.notify_test
  if (typeof tester !== 'function') {
    monitorState.testResult = '浏览器模式：只能演示页面内弹窗和红框；桌面版会同时弹系统通知。'
    return
  }
  try {
    const channel = String(await tester('alert'))
    monitorState.testResult = `桌面提醒已通过「${CHANNEL[channel] ?? channel}」发出。`
  } catch {
    monitorState.testResult = '桌面提醒调用失败，页面内弹窗仍然有效。'
  }
}

async function poll() {
  try {
    const payload = await api.monitorEvents(monitorState.lastId)
    monitorState.alertsActive = payload.alerts_active !== false
    monitorState.statusText = payload.status_text ?? ''
    if (monitorState.alertsActive) for (const event of payload.items) show(payload.date, event)
    else {
      // 收盘 / 午休 / 休市：清掉之前留下的提醒弹窗和红框演示，不再弹
      // 「测试提醒」是用户主动点的（id 为负），保留演示
      monitorState.toasts = monitorState.toasts.filter((toast) => toast.id < 0)
    }
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
