import { api } from '@/api/client'
import type { WatchItem, WatchMonitor } from '@/api/types'
import { show } from '@/stores/monitor'
import { reactive } from 'vue'

/** 自选股共享状态：自选股页、实时监控页、总览卡片共用一份，全局只有一个轮询。 */
export const watch = reactive({
  items: [] as WatchItem[],
  monitor: null as WatchMonitor | null,
  loaded: false,
  started: false,
})

let timer: number | undefined

export async function loadWatchlist() {
  watch.items = (await api.watchlist().catch(() => ({ items: watch.items }))).items
  watch.loaded = true
}

export function setItems(items: WatchItem[]) {
  watch.items = items
  void refreshMonitor()
}

export async function refreshMonitor() {
  if (!watch.items.length) {
    watch.monitor = null
    return
  }
  const snap = await api.watchlistMonitor().catch(() => null)
  if (!snap) return
  watch.monitor = snap
  if (snap.alerts_active) {
    for (const row of snap.items) {
      if (row.level === 'normal') continue
      show(snap.date, { id: Date.now() + Math.random(), code: row.code, name: row.name, level: row.level, pct: row.pct, rank: null, at: null }, '自选股 ')
    }
  }
}

/** 应用启动时调用一次：交易中 5 秒一轮，其余 60 秒（一次批量请求）。 */
export function startWatchlist() {
  if (watch.started || import.meta.env.VITE_USE_MOCK === '1') return
  watch.started = true
  const tick = async () => {
    if (!watch.loaded) await loadWatchlist()
    await refreshMonitor()
    const fast = watch.monitor?.phase === 'trading'
    timer = window.setTimeout(tick, fast ? 5000 : 60000)
  }
  void tick()
}

export function stopWatchlist() {
  if (timer) window.clearTimeout(timer)
  watch.started = false
}
