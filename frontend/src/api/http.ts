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

/**
 * 响应体不是 `{"detail": …}` 时给页面的那句话。
 * 后端崩了时 uvicorn 只回一行英文「Internal Server Error」，原样贴到页面上等于没说；
 * 只有带中文的纯文本才是我们自己写的说明，才照搬。
 */
export function plainDetail(status: number, raw: string): string {
  const text = raw.trim()
  if (text && /[\u4e00-\u9fff]/.test(text) && !text.startsWith('<')) return text
  return status >= 500 ? `后端出错（${status}），详情见日志` : `请求失败（${status}）`
}

function useMock(): boolean {
  return import.meta.env.VITE_USE_MOCK === '1'
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (useMock()) {
    const { mockRequest } = await import('@/mocks/server')
    return mockRequest<T>(path, init)
  }

  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })
  const text = await response.text()
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
        : plainDetail(response.status, typeof payload === 'string' ? payload : '')
    throw new ApiError(response.status, detail)
  }

  return payload as T
}
