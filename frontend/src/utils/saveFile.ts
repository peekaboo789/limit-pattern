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
export async function saveExportedText(filename: string, text: string, blob?: Blob): Promise<SaveMode> {
  const saveFile = window.pywebview?.api?.save_file
  if (typeof saveFile === 'function') {
    const result = await saveFile(filename, text)
    if (result === false) return 'cancel'
    return 'dialog'
  }
  triggerBlobDownload(filename, blob ?? new Blob([text], { type: 'text/plain;charset=utf-8' }))
  return 'download'
}
