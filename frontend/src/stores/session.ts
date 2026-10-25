import { api, ApiError } from '@/api/client'
import type { Health, Settings, SyncStatus } from '@/api/types'
import { reactive } from 'vue'

export const session = reactive({
  health: null as Health | null,
  healthError: '',
  settings: null as Settings | null,
  sync: null as SyncStatus | null,
  syncError: '',
  startingSync: false,
})

/** 花钱的按钮只在这三项配齐时才该按得动——判据与后端 `ai_ready()` 同一个字段。 */
export const aiReady = () => session.settings?.ai_enabled === true
/** 「最近 N 个交易日」。N 就是设置里的对比窗口，设置还没读到时不编一个 3 出来。 */
export const compareWindowText = () => {
  const days = session.settings?.compare_window_trading_days
  return days ? `最近 ${days} 个交易日` : '对比窗口内'
}

export async function loadHealth(): Promise<void> {
  try {
    session.health = await api.health()
    session.healthError = ''
  } catch (error) {
    session.healthError = error instanceof ApiError ? error.detail : '无法连接后端'
  }
}

export async function loadSettings(): Promise<void> {
  try {
    session.settings = await api.settings()
  } catch {
    // 读不到就保持上一份：新页面只因此少一颗按钮，不该顶出红条
  }
}

export async function loadSync(): Promise<void> {
  try {
    session.sync = await api.syncStatus()
    session.syncError = ''
  } catch (error) {
    session.syncError = error instanceof ApiError ? error.detail : '无法获取同步状态'
  }
}

let timer = 0

export function stopSyncPoll(): void {
  if (!timer) return
  window.clearInterval(timer)
  timer = 0
}

function ensureSyncPoll(): void {
  if (timer) return
  timer = window.setInterval(() => {
    void loadSync().then(() => {
      if (session.sync && !session.sync.running) {
        stopSyncPoll()
        void loadHealth()
      }
    })
  }, 700)
}

export async function startSync(): Promise<void> {
  session.startingSync = true
  session.syncError = ''
  try {
    await api.startSync()
    await loadSync()
    ensureSyncPoll()
  } catch (error) {
    session.syncError = error instanceof ApiError ? error.detail : '同步启动失败'
  } finally {
    session.startingSync = false
  }
}

export async function initSession(): Promise<void> {
  await Promise.all([loadHealth(), loadSettings(), loadSync()])
  if (session.sync?.running) ensureSyncPoll()
}
