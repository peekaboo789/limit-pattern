import { ref } from 'vue'
import type { CouncilSession } from '../api/types'

interface HistoryApi {
  councilHistory: (params: { limit: number }, signal?: AbortSignal) => Promise<{ items: CouncilSession[] }>
  councilGet: (id: number, signal?: AbortSignal) => Promise<CouncilSession>
  councilDelete: (id: number) => Promise<unknown>
}
function message(reason: unknown, fallback: string) {
  return String((reason as { detail?: string })?.detail || (reason instanceof Error ? reason.message : '') || fallback)
}

/** Per-record revisions prevent an old history/detail response undoing a newer mutation. */
export function useCouncilHistory(api: HistoryApi) {
  const sessions = ref<CouncilSession[]>([])
  const expanded = ref<number | null>(null)
  const loading = ref(false)
  const error = ref('')
  const detailError = ref<Record<number, string>>({})
  const detailReady = ref<Record<number, string>>({})
  const detailLoading = ref<Record<number, boolean>>({})
  const deleting = ref<Record<number, boolean>>({})
  const deleted = new Map<number, string | null>()
  const deleteError = ref<Record<number, string>>({})
  const versions = new Map<number, number>()
  const tickets = new Map<number, number>()
  const loads = new Map<number, number>()
  const requests = new Map<number, AbortController>()
  let listRequest: AbortController | null = null
  const opening = ref<number | null>(null)
  let revision = 0
  let historyTicket = 0
  let disposed = false

  function replaceSession(row: CouncilSession) {
    if (disposed || deleted.has(row.id)) return
    detailReady.value[row.id] = row.created_at || 'legacy'
    versions.set(row.id, ++revision)
    tickets.set(row.id, (tickets.get(row.id) || 0) + 1)
    const index = sessions.value.findIndex(item => item.id === row.id)
    if (index < 0) sessions.value.unshift(row)
    else sessions.value[index] = row
  }
  async function loadSessions() {
    if (disposed) return
    listRequest?.abort()
    listRequest = new AbortController()
    const ticket = ++historyTicket
    const stamp = revision
    loading.value = true
    try {
      const data = await api.councilHistory({ limit: 100 }, listRequest.signal)
      if (disposed || ticket !== historyTicket) return
      const current = new Map(sessions.value.map(row => [row.id, row]))
      const rows = data.items.filter(row => {
        if (!deleted.has(row.id)) return true
        if ((row.created_at ?? null) === deleted.get(row.id)) return false
        // A different persisted creation instant is a new SQLite incarnation, possibly
        // created in another tab. Pre-delete history requests were invalidated below.
        deleted.delete(row.id)
        tickets.set(row.id, (tickets.get(row.id) || 0) + 1)
        return true
      }).map(row =>
        (versions.get(row.id) || 0) > stamp ? (current.get(row.id) || row) : row)
      const ids = new Set(rows.map(row => row.id))
      for (const row of sessions.value) if (!ids.has(row.id) && !deleted.has(row.id) && (versions.get(row.id) || 0) > stamp) rows.unshift(row)
      sessions.value = rows
      error.value = ''
    } catch (reason) {
      if (!disposed && ticket === historyTicket) error.value = message(reason, '讨论记录加载失败，请重试')
    } finally {
      if (!disposed && ticket === historyTicket) loading.value = false
    }
  }
  async function loadSession(id: number) {
    if (disposed || deleted.has(id)) return
    requests.get(id)?.abort()
    const controller = new AbortController()
    requests.set(id, controller)
    const ticket = (tickets.get(id) || 0) + 1
    tickets.set(id, ticket)
    loads.set(id, ticket)
    detailLoading.value[id] = true
    detailError.value[id] = ''
    try {
      const row = await api.councilGet(id, controller.signal)
      if (disposed || deleted.has(id) || tickets.get(id) !== ticket) return
      replaceSession(row)
    } catch (reason) {
      if (!disposed && !deleted.has(id) && tickets.get(id) === ticket) detailError.value[id] = message(reason, '会话加载失败，请重试')
    } finally {
      // A replacement invalidates this ticket; only an actual newer GET keeps loading alive.
      if (!disposed && loads.get(id) === ticket) { detailLoading.value[id] = false; if(opening.value===id)opening.value=null }
      if(requests.get(id)===controller)requests.delete(id)
    }
  }
  function close() {
    if (expanded.value !== null) cancelDetail(expanded.value)
    expanded.value = null; opening.value = null
  }
  async function open(row: CouncilSession) {
    if (expanded.value !== null) cancelDetail(expanded.value)
    if (expanded.value === row.id) { expanded.value = null; opening.value=null; return }
    expanded.value = row.id
    opening.value = row.id
    await loadSession(row.id) // Completed history is refreshed too, not just running rows.
  }
  async function deleteSession(id: number, confirm: () => boolean) {
    if (disposed || deleted.has(id) || deleting.value[id] || !confirm()) return false
    deleting.value[id] = true
    deleteError.value[id] = ''
    detailError.value[id] = ''
    try {
      await api.councilDelete(id)
      if (disposed) return false
      cancelDetail(id)
      listRequest?.abort()
      deleted.set(id, sessions.value.find(row => row.id === id)?.created_at ?? null)
      ++historyTicket // Any pre-delete list snapshot is now stale.
      loading.value = false
      tickets.set(id, (tickets.get(id) || 0) + 1)
      versions.set(id, ++revision)
      sessions.value = sessions.value.filter(row => row.id !== id)
      if (expanded.value === id) expanded.value = null
      delete deleteError.value[id]
      delete detailError.value[id]
      delete detailLoading.value[id]
      delete detailReady.value[id]
      return true
    } catch (reason) {
      if (!disposed) { deleteError.value[id] = message(reason, '删除失败，记录已保留，请重试'); detailError.value[id] = deleteError.value[id] }
      return false
    } finally {
      if (!disposed) deleting.value[id] = false
    }
  }
  function acceptNewSession(id: number) {
    // SQLite may reuse the highest deleted INTEGER PK. Only an explicit new-run response
    // may remove its tombstone; invalidate every request from the former incarnation.
    deleted.delete(id)
    delete detailReady.value[id]
    tickets.set(id, (tickets.get(id) || 0) + 1)
    versions.set(id, ++revision)
    ++historyTicket
  }
  function cancelDetail(id: number) { requests.get(id)?.abort();requests.delete(id);tickets.set(id,(tickets.get(id)||0)+1);detailLoading.value[id]=false }
  function dispose() { disposed = true; ++historyTicket; listRequest?.abort(); requests.forEach(request=>request.abort()); requests.clear(); expanded.value = null }
  return { sessions, expanded, opening, loading, error, detailError, detailLoading, detailReady, deleting, deleteError, isDeleted: (id: number) => deleted.has(id),
    loadSessions, loadSession, replaceSession, open, close, deleteSession, acceptNewSession, dispose }
}

/** setTimeout AFTER each request completes: no overlapping interval requests or leaked restart. */
export function createCouncilPoll(task: (current: () => boolean) => Promise<boolean>, delay = 1500) {
  let timer: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  let active = false
  async function tick(token: number) {
    const current = () => active && token === generation
    if (!current()) return
    const again = await task(current)
    if (!current()) return
    if (again) timer = setTimeout(() => { void tick(token) }, delay)
    else active = false
  }
  return {
    start() { if (active) return; active = true; void tick(++generation) },
    stop() { active = false; ++generation; if (timer !== undefined) clearTimeout(timer); timer = undefined },
  }
}
