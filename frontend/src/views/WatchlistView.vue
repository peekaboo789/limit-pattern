<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { WatchCandidate, WatchPreview } from '@/api/types'
import WatchlistTable from '@/components/WatchlistTable.vue'
import { canOpenFolder, lastSaved, openFolder, saveExportedText } from '@/utils/saveFile'
import { loadWatchlist, setItems, watch as wl } from '@/stores/watchlist'
import { computed, onMounted, ref } from 'vue'

const q = ref('')
const suggestions = ref<WatchCandidate[]>([])
const active = ref(0)
const msg = ref('')
const error = ref('')
const selected = ref<Set<string>>(new Set())
let searchSerial = 0
let searchTimer: number | undefined

function onInput() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(async () => {
    const serial = ++searchSerial
    const text = q.value.trim()
    if (!text) {
      suggestions.value = []
      return
    }
    const r = await api.watchlistSearch(text).catch(() => ({ items: [] }))
    if (serial === searchSerial) {
      suggestions.value = r.items
      active.value = 0
    }
  }, 120)
}

async function add(list: { code: string; name?: string }[]) {
  error.value = ''
  try {
    const r = await api.watchlistAdd(list)
    setItems(r.items)
    msg.value = `已加入 ${r.added.length} 只` + (r.duplicates.length ? `，${r.duplicates.length} 只已在自选（${r.duplicates.join('、')}）` : '')
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : '加入失败'
  }
}

async function pick(c: WatchCandidate) {
  await add([{ code: c.code, name: c.name }])
  q.value = ''
  suggestions.value = []
}

async function onEnter() {
  if (searchTimer) {
    window.clearTimeout(searchTimer)
    const r = await api.watchlistSearch(q.value.trim()).catch(() => ({ items: [] }))
    suggestions.value = r.items
  }
  if (suggestions.value.length === 1 || (suggestions.value.length && active.value >= 0)) await pick(suggestions.value[active.value] ?? suggestions.value[0])
  else if (q.value.trim()) error.value = `没找到「${q.value.trim()}」`
}

function move(d: number) {
  if (suggestions.value.length) active.value = (active.value + d + suggestions.value.length) % suggestions.value.length
}

async function remove(codes: string[]) {
  setItems((await api.watchlistDelete(codes)).items)
  for (const c of codes) selected.value.delete(c)
  msg.value = `已删除 ${codes.length} 只`
}

async function clearAll() {
  if (!window.confirm(`确定清空全部 ${wl.items.length} 只自选股？`)) return
  setItems((await api.watchlistDelete([], true)).items)
  selected.value = new Set()
  msg.value = '已清空'
}

function toggle(code: string) {
  const next = new Set(selected.value)
  if (next.has(code)) next.delete(code)
  else next.add(code)
  selected.value = next
}
const allChecked = computed(() => wl.items.length > 0 && selected.value.size === wl.items.length)
function toggleAll() {
  selected.value = allChecked.value ? new Set() : new Set(wl.items.map((x) => x.code))
}

async function saveNote(code: string, note: string) {
  setItems((await api.watchlistNote(code, note)).items)
}

async function shift(index: number, d: number) {
  const codes = wl.items.map((x) => x.code)
  const j = index + d
  if (j < 0 || j >= codes.length) return
  ;[codes[index], codes[j]] = [codes[j], codes[index]]
  setItems((await api.watchlistReorder(codes)).items)
}

// ---------- 一键导入 ----------
const importOpen = ref(false)
const importText = ref('')
const preview = ref<WatchPreview | null>(null)
const choices = ref<Record<string, string>>({})
const previewing = ref(false)

async function readFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const buf = await file.arrayBuffer()
  let text = new TextDecoder('utf-8').decode(buf)
  if (text.includes('\ufffd')) text = new TextDecoder('gbk').decode(buf) // 同花顺 / 通达信导出常是 GBK
  importText.value = text
  await doPreview()
}

async function doPreview() {
  if (!importText.value.trim()) return
  previewing.value = true
  error.value = ''
  try {
    preview.value = await api.watchlistPreview(importText.value)
    choices.value = Object.fromEntries(preview.value.ambiguous.map((a) => [a.token, '']))
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : '解析失败'
  } finally {
    previewing.value = false
  }
}

const toImport = computed(() => {
  if (!preview.value) return []
  const picked = preview.value.ambiguous
    .map((a) => a.candidates.find((c) => c.code === choices.value[a.token]))
    .filter((c): c is WatchCandidate => Boolean(c))
  const seen = new Set<string>()
  return [...preview.value.matched, ...picked].filter((c) => !seen.has(c.code) && Boolean(seen.add(c.code)))
})

async function confirmImport() {
  await add(toImport.value.map((c) => ({ code: c.code, name: c.name })))
  preview.value = null
  importText.value = ''
  importOpen.value = false
}

async function addFromRanking() {
  const r = await api.aiRanking().catch(() => null)
  if (!r?.items.length) {
    error.value = '还没有 AI 榜单'
    return
  }
  await add(r.items.map((i) => ({ code: i.code, name: i.name })))
  msg.value = `AI 榜单 ${r.date}：` + msg.value
}

async function addFromSnapshot() {
  const s = await api.manualSnapshot().catch(() => null)
  if (!s?.result.items.length) {
    error.value = '还没有手动采集结果'
    return
  }
  await add(s.result.items.map((i) => ({ code: i.code, name: i.name })))
  msg.value = `${s.label} 前 ${s.result.items.length}：` + msg.value
}

// ---------- 导出 ----------
const savedPath = ref<string | null>(null)
async function exportList(format: 'txt' | 'csv') {
  const file = await api.watchlistExport(format)
  const mode = await saveExportedText(file.filename, file.text, file.blob)
  savedPath.value = lastSaved.path
  msg.value = mode === 'cancel' ? '已取消保存' : mode === 'dialog' ? `已保存到 ${lastSaved.path ?? file.filename}` : `已下载 ${file.filename}`
}

onMounted(async () => {
  if (!wl.loaded) await loadWatchlist()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>自选股</h1>
      <p class="lede">输入代码、名称或拼音首字母添加；一键导入同花顺 / 东方财富导出或粘贴的列表；交易时段实时监控，涨幅 ≥3% 提醒、≥6% 红框。</p>
    </div>
  </header>

  <section class="card">
    <div class="toolbar watch-add">
      <div class="search-box">
        <input v-model="q" class="search-input" placeholder="代码 / 名称 / 拼音首字母，例如 600132、重庆啤、cqpj" @input="onInput"
               @keydown.enter.prevent="onEnter" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" />
        <ul v-if="suggestions.length" class="suggest">
          <li v-for="(c, i) in suggestions" :key="c.code" :class="{ on: i === active }" @mousedown.prevent="pick(c)">
            <span class="code">{{ c.code }}</span> {{ c.name || '（本地没有名称）' }} <span class="muted small">{{ c.board }}<template v-if="c.star">，不在相似度样本池</template></span>
          </li>
        </ul>
      </div>
      <button class="btn" type="button" :disabled="!q.trim()" @click="onEnter">添加</button>
      <button class="btn btn-ghost" type="button" @click="importOpen = !importOpen">一键导入</button>
      <button class="btn btn-ghost" type="button" @click="addFromRanking">加入 AI 榜单</button>
      <button class="btn btn-ghost" type="button" @click="addFromSnapshot">加入手动采集前 100</button>
      <button class="btn btn-ghost" type="button" :disabled="!wl.items.length" @click="exportList('txt')">导出 TXT</button>
      <button class="btn btn-ghost" type="button" :disabled="!wl.items.length" @click="exportList('csv')">导出 CSV</button>
    </div>
    <p v-if="msg" class="hint">{{ msg }} <button v-if="savedPath && canOpenFolder()" class="btn btn-ghost" @click="openFolder(savedPath)">打开所在文件夹</button></p>
    <p v-if="error" class="alert">{{ error }}</p>
  </section>

  <section v-if="importOpen" class="card import-card">
    <div class="card-head"><h2>一键导入</h2></div>
    <p class="muted small">支持逗号、空格、换行、Tab 分隔的代码或名称；SH600132 / 600132.SH / 「600132 重庆啤酒」这种同花顺、东方财富导出的行；外部 AI 的 JSON（items 或 stocks 列表）。</p>
    <textarea v-model="importText" rows="6" style="width: 100%; font-family: monospace" placeholder="600132 重庆啤酒&#10;SZ000001&#10;永安药业, 宁德时代" />
    <div class="toolbar" style="margin-top: 8px">
      <label class="btn btn-ghost">选择 TXT / CSV 文件<input type="file" accept=".txt,.csv,.json,text/plain" hidden @change="readFile" /></label>
      <button class="btn" type="button" :disabled="previewing || !importText.trim()" @click="doPreview">{{ previewing ? '解析中…' : '预览' }}</button>
    </div>
    <div v-if="preview" class="preview">
      <p><b>识别 {{ preview.token_count }} 项：</b>匹配 {{ preview.matched.length }}，待选 {{ preview.ambiguous.length }}，找不到 {{ preview.not_found.length }}，重复 {{ preview.duplicates.length }}</p>
      <div v-if="preview.matched.length" class="group"><h3>匹配</h3>
        <span v-for="m in preview.matched" :key="m.code" class="chip">{{ m.code }} {{ m.name }}<template v-if="m.star">（科创板）</template><span v-if="m.token !== m.code" class="muted small"> ← {{ m.token }}</span></span>
      </div>
      <div v-if="preview.ambiguous.length" class="group"><h3>有多个候选，请选一只</h3>
        <label v-for="a in preview.ambiguous" :key="a.token" class="field">「{{ a.token }}」
          <select v-model="choices[a.token]">
            <option value="">跳过</option>
            <option v-for="c in a.candidates" :key="c.code" :value="c.code">{{ c.code }} {{ c.name }}</option>
          </select>
        </label>
      </div>
      <div v-if="preview.not_found.length" class="group"><h3>找不到</h3>
        <span v-for="n in preview.not_found" :key="n.token" class="chip warn">{{ n.token }}（{{ n.reason }}）</span>
      </div>
      <div v-if="preview.duplicates.length" class="group"><h3>重复</h3>
        <span v-for="d in preview.duplicates" :key="d.token + d.code" class="chip muted">{{ d.code }} {{ d.name }}（{{ d.reason }}）</span>
      </div>
      <button class="btn" type="button" :disabled="!toImport.length" @click="confirmImport">确认导入 {{ toImport.length }} 只</button>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>我的自选（{{ wl.items.length }}）</h2>
      <span class="toolbar">
        <button class="btn btn-ghost" type="button" :disabled="!selected.size" @click="remove([...selected])">删除选中（{{ selected.size }}）</button>
        <button class="btn btn-ghost" type="button" :disabled="!wl.items.length" @click="clearAll">清空</button>
      </span>
    </div>
    <div v-if="!wl.items.length" class="empty">还没有自选股</div>
    <div v-else class="table-wrap">
      <table class="watch-list">
        <thead><tr><th><input type="checkbox" :checked="allChecked" @change="toggleAll" /></th><th>股票</th><th>板块</th><th>备注</th><th>加入时间</th><th>排序</th><th></th></tr></thead>
        <tbody>
          <tr v-for="(x, i) in wl.items" :key="x.code">
            <td><input type="checkbox" :checked="selected.has(x.code)" @change="toggle(x.code)" /></td>
            <td>{{ x.name || '—' }} <span class="code muted">{{ x.code }}</span></td>
            <td>{{ x.code.startsWith('688') || x.code.startsWith('689') ? '科创板（不在相似度样本池）' : x.code.startsWith('30') ? '创业板' : '主板' }}</td>
            <td><input class="note" :value="x.note" placeholder="备注" @change="saveNote(x.code, ($event.target as HTMLInputElement).value)" /></td>
            <td class="muted small">{{ x.added_at?.slice(0, 16).replace('T', ' ') }}</td>
            <td><button class="btn btn-ghost mini" :disabled="i === 0" @click="shift(i, -1)">↑</button><button class="btn btn-ghost mini" :disabled="i === wl.items.length - 1" @click="shift(i, 1)">↓</button></td>
            <td><button class="btn btn-ghost mini" @click="remove([x.code])">删除</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <WatchlistTable />
</template>

<style scoped>
.watch-add { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.search-box { position: relative; flex: 1; min-width: 320px; }
.search-input { width: 100%; }
.suggest { position: absolute; z-index: 20; left: 0; right: 0; top: 100%; margin: 2px 0 0; padding: 4px 0; list-style: none; background: #111827; border: 1px solid rgba(160, 186, 224, 0.25); border-radius: 8px; max-height: 320px; overflow: auto; }
.suggest li { padding: 6px 10px; cursor: pointer; }
.suggest li.on, .suggest li:hover { background: rgba(232, 184, 109, 0.14); }
.preview .group { margin: 8px 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.preview h3 { font-size: 13px; margin: 0 8px 0 0; width: 100%; }
.chip.warn { color: #f59e0b; }
.note { width: 12em; }
.mini { padding: 2px 8px; font-size: 12px; }
</style>
