import { api, ApiError } from '@/api/client'
import type { Health, SyncStatus } from '@/api/types'
import { reactive } from 'vue'

export const session = reactive({
  health: null as Health | null,
  healthError: '',
  sync: null as SyncStatus | null,
  syncError: '',
  startingSync: false,
})

export async function loadHealth(): Promise<void> {
  try {
    session.health = await api.health()
    session.healthError = ''
  } catch (error) {
    session.healthError = error instanceof ApiError ? error.detail : '无法连接后端'
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
  }, 1000)
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
  await Promise.all([loadHealth(), loadSync()])
  if (session.sync?.running) ensureSyncPoll()
}

export async function cancelSync(): Promise<void> {
  try {
    session.sync = await api.cancelSync()
    ensureSyncPoll()
  } catch (error) {
    session.syncError = error instanceof ApiError ? error.detail : '取消失败'
  }
}
