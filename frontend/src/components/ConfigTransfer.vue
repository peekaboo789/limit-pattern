<script setup lang="ts">
import { request } from '@/api/http'
import { filenameFromDisposition, saveExportedText } from '@/utils/saveFile'
import { ref } from 'vue'

interface Change { key: string; label: string; old: string; new: string; secret: boolean }
interface Preview { app_version: string; exported_at: string; includes_secrets: boolean; changes: Change[]; watchlist_count: number | null; has_account: boolean }

const withSecrets = ref(false)
const withWatchlist = ref(true)
const exporting = ref(false)
const msg = ref<{ ok: boolean; text: string } | null>(null)
const file = ref<unknown>(null)
const fileName = ref('')
const preview = ref<Preview | null>(null)
const applyWatchlist = ref(true)
const applyAccount = ref(false)
const applying = ref(false)

async function doExport() {
  exporting.value = true
  msg.value = null
  try {
    const res = await fetch(`/api/config/export?secrets=${withSecrets.value}&watchlist=${withWatchlist.value}`)
    if (!res.ok) throw new Error(`导出失败（${res.status}）`)
    const text = await res.text()
    const name = filenameFromDisposition(res.headers.get('content-disposition')) || '涨停形态系统_配置.json'
    const mode = await saveExportedText(name, text, new Blob([text], { type: 'application/json;charset=utf-8' }))
    if (mode !== 'cancel') msg.value = { ok: true, text: `已导出 ${name}${withSecrets.value ? '（含密钥，请妥善保管）' : '（不含密钥）'}` }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '导出失败' }
  } finally {
    exporting.value = false
  }
}

async function pick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  msg.value = null
  preview.value = null
  fileName.value = f.name
  try {
    file.value = JSON.parse(await f.text())
  } catch {
    msg.value = { ok: false, text: '不是合法的 JSON 文件' }
    return
  }
  try {
    preview.value = await request<Preview>('/api/config/import/preview', { method: 'POST', body: JSON.stringify({ file: file.value }) })
  } catch (err) {
    msg.value = { ok: false, text: err instanceof Error ? err.message : '预览失败' }
  }
}

async function doImport() {
  applying.value = true
  try {
    const r = await request<{ backup: string; watchlist: number | null; account: boolean }>('/api/config/import', {
      method: 'POST', body: JSON.stringify({ file: file.value, watchlist: applyWatchlist.value, account: applyAccount.value }) })
    msg.value = { ok: true, text: `已导入。原设置已备份到 backups/${r.backup}${r.watchlist != null ? `；自选股 ${r.watchlist} 只` : ''}。页面将刷新。` }
    preview.value = null
    window.setTimeout(() => window.location.reload(), 1500)
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '导入失败' }
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <section class="card config-transfer">
    <div class="card-head"><h2>配置导出 / 导入</h2></div>
    <p class="hint">一键导出全部设置（分析参数、报警开关、数据源、企业微信、AI 模型列表）为 JSON，换电脑 / 部署 Docker 时导入即可。默认不含任何密钥。</p>
    <div class="row">
      <label class="check"><input v-model="withWatchlist" type="checkbox" /> 包含自选股</label>
      <label class="check danger"><input v-model="withSecrets" type="checkbox" /> 包含密钥（请妥善保管）</label>
      <button class="btn" type="button" :disabled="exporting" @click="doExport">{{ exporting ? '导出中…' : '导出配置' }}</button>
    </div>
    <p v-if="withSecrets" class="hint warn">勾选后文件里会有 AI API Key、企业微信 Secret / Token / EncodingAESKey 和登录密码哈希，任何拿到文件的人都能用。</p>
    <div class="row" style="margin-top: 12px">
      <label class="btn btn-ghost file">导入配置…<input type="file" accept=".json,application/json" @change="pick" /></label>
      <span class="muted">{{ fileName }}</span>
    </div>
    <div v-if="preview" class="preview">
      <p class="hint">文件来自 v{{ preview.app_version }}，导出于 {{ preview.exported_at }}，{{ preview.includes_secrets ? '含密钥' : '不含密钥（现有密钥保留不变）' }}。将改动 {{ preview.changes.length }} 项：</p>
      <table v-if="preview.changes.length">
        <thead><tr><th>设置</th><th>当前</th><th>导入后</th></tr></thead>
        <tbody><tr v-for="c in preview.changes" :key="c.key"><td>{{ c.label }}</td><td class="muted">{{ c.old }}</td><td>{{ c.new }}</td></tr></tbody>
      </table>
      <p v-else class="hint">设置没有差异。</p>
      <label v-if="preview.watchlist_count != null" class="check"><input v-model="applyWatchlist" type="checkbox" /> 同时替换自选股（{{ preview.watchlist_count }} 只）</label>
      <label v-if="preview.has_account" class="check"><input v-model="applyAccount" type="checkbox" /> 同时导入登录账号（导入后要重新登录）</label>
      <div class="row" style="margin-top: 8px">
        <button class="btn" type="button" :disabled="applying" @click="doImport">{{ applying ? '导入中…' : '确认导入（先自动备份当前设置）' }}</button>
        <button class="btn btn-ghost" type="button" @click="preview = null">取消</button>
      </div>
    </div>
    <p v-if="msg" class="hint" :style="{ color: msg.ok ? '#16a34a' : '#dc2626' }">{{ msg.text }}</p>
  </section>
</template>

<style scoped>
.row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
.check { display: inline-flex; gap: 6px; align-items: center; }
.danger { color: #f59e0b; }
.warn { color: #f59e0b; }
.file { position: relative; overflow: hidden; cursor: pointer; }
.file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.preview { margin-top: 12px; }
.preview table { width: 100%; border-collapse: collapse; font-size: 13px; }
.preview th, .preview td { text-align: left; padding: 5px 8px; border-top: 1px solid rgba(127, 127, 127, 0.2); word-break: break-all; }
</style>
