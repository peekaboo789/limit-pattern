export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export function formatDetail(detail: unknown): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: unknown }).msg)
        }
        return JSON.stringify(item)
      })
      .join('；')
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return '请求失败'
}

function useMock(): boolean {
  return import.meta.env.VITE_USE_MOCK === '1'
}

export async function request<T>(path: string, init?: RequestInit, timeoutMs = 20000): Promise<T> {
  if (useMock()) {
    const { mockRequest } = await import('@/mocks/server')
    return mockRequest<T>(path, init)
  }

  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  // 每个请求都有超时，后端卡住时页面也不会一直等。
  const controller = new AbortController()
  const outer = init?.signal
  if (outer) outer.addEventListener('abort', () => controller.abort(), { once: true })
  let timedOut = false
  const timer = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  let response: Response
  let text: string
  try {
    response = await fetch(path, { ...init, headers, signal: controller.signal })
    text = await response.text()
  } catch (error) {
    if (timedOut) throw new ApiError(504, `请求超时（${Math.round(timeoutMs / 1000)} 秒没有响应）`)
    throw error
  } finally {
    window.clearTimeout(timer)
  }
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text) as unknown
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? formatDetail((payload as { detail: unknown }).detail)
        : typeof payload === 'string' && payload
          ? payload
          : `请求失败（${response.status}）`
    if (response.status === 401 && payload && typeof payload === 'object' && 'login_required' in payload) {
      window.dispatchEvent(new CustomEvent('lp-login-required'))
    }
    throw new ApiError(response.status, detail)
  }

  return payload as T
}
