export type SaveMode = 'dialog' | 'download' | 'cancel'

export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null
  const encoded = /filename\*\s*=\s*[^']*''([^;]+)/i.exec(header)
  if (encoded?.[1]) {
    const raw = encoded[1].trim().replace(/^"|"$/g, '')
    try {
      return decodeURIComponent(raw)
    } catch {
      return raw
    }
  }
  const quoted = /filename\s*=\s*"([^"]+)"/i.exec(header)
  if (quoted?.[1]) return quoted[1].trim()
  const plain = /filename\s*=\s*([^;]+)/i.exec(header)
  return plain?.[1]?.trim().replace(/^"|"$/g, '') || null
}

export function defaultExportFilename(type: string, date: string, code?: string, top?: number): string {
  if (type === 'scan') return `scan-${date}-top${top ?? 50}.txt`
  if (type === 'similarity') return `similarity-${code ?? 'unknown'}-${date}-top${top ?? 20}.txt`
  if (type === 'pool') return `pool-${date}.txt`
  if (type === 'next_day_pool') return `next_day_pool-${date}.txt`
  if (type === 'cluster') return `cluster-${date}.txt`
  if (type === 'backtest') return `backtest-${date}.txt`
  if (type === 'ai_ranking') return `ai_ranking-${date}.txt`
  return `summary-${date}.txt`
}

function triggerBlobDownload(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/** 桌面壳优先走保存对话框；浏览器则用 Blob 触发下载。 */
export const lastSaved: { path: string | null } = { path: null }

export async function saveExportedText(filename: string, text: string, blob?: Blob): Promise<SaveMode> {
  const saveFile = window.pywebview?.api?.save_file
  lastSaved.path = null
  if (typeof saveFile === 'function') {
    const result = await saveFile(filename, text)
    if (result === false || result === null || result === undefined) return 'cancel'
    if (typeof result === 'string') lastSaved.path = result
    return 'dialog'
  }
  triggerBlobDownload(filename, blob ?? new Blob([text], { type: 'text/plain;charset=utf-8' }))
  return 'download'
}

/** 桌面程序里打开导出文件所在文件夹；浏览器里没有这个能力。 */
export function canOpenFolder(): boolean {
  return typeof (window.pywebview?.api as { open_folder?: unknown } | undefined)?.open_folder === 'function'
}

export async function openFolder(path: string): Promise<boolean> {
  const api = window.pywebview?.api as { open_folder?: (p: string) => Promise<boolean> } | undefined
  return api?.open_folder ? Boolean(await api.open_folder(path)) : false
}
