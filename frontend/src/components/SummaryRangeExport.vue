<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import { canOpenFolder, lastSaved, openFolder, saveExportedText } from '@/utils/saveFile'
import { computed, onMounted, ref, watch } from 'vue'

/** 每日总结按区间一键导出。dates：已有总结的日期（新到旧），用来算「近 N 交易日」。 */
const props = defineProps<{ dates: string[] }>()
type Format = 'txt' | 'md' | 'json' | 'csv'
const start = ref('')
const end = ref('')
const format = ref<Format>('txt')
const preset = ref('20')
const earliest = ref<string | null>(null)
const busy = ref(false)
const hint = ref('')
const error = ref('')
const savedPath = ref<string | null>(null)

const sortedDesc = computed(() => [...props.dates].sort().reverse())
const today = () => new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)

function apply(name: string) {
  preset.value = name
  const latest = sortedDesc.value[0] ?? today()
  if (name === 'month') {
    end.value = latest
    start.value = latest.slice(0, 8) + '01'
  } else if (name === 'all') {
    start.value = earliest.value ?? sortedDesc.value[sortedDesc.value.length - 1] ?? latest
    end.value = latest
  } else {
    const n = Number(name)
    end.value = latest
    start.value = sortedDesc.value[Math.min(n, sortedDesc.value.length) - 1] ?? latest
  }
}

watch(
  () => props.dates.length,
  (n, old) => {
    if (n && !old) apply(preset.value)
  },
)

onMounted(async () => {
  earliest.value = (await api.summaryRange().catch(() => ({ earliest: null }))).earliest
  if (props.dates.length) apply(preset.value)
})

const valid = computed(() => /^\d{4}-\d{2}-\d{2}$/.test(start.value) && /^\d{4}-\d{2}-\d{2}$/.test(end.value) && start.value <= end.value)

async function run() {
  if (!valid.value || busy.value) return
  busy.value = true
  hint.value = ''
  error.value = ''
  savedPath.value = null
  try {
    const file = await api.summaryExport(start.value, end.value, format.value)
    const mode = await saveExportedText(file.filename, file.text, file.blob)
    savedPath.value = lastSaved.path
    const what = `${file.count} 天总结${file.missing ? `，${file.missing} 个交易日缺总结（文件开头列出）` : ''}`
    hint.value = mode === 'cancel' ? '已取消保存' : mode === 'dialog' ? `已保存到 ${lastSaved.path ?? file.filename}（${what}）` : `已下载 ${file.filename}（${what}）`
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '导出失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="card range-export">
    <div class="card-head"><h2>导出每日总结</h2></div>
    <div class="toolbar">
      <span class="presets">
        <button v-for="p in [['5', '近 5 交易日'], ['20', '近 20 交易日'], ['60', '近 60 交易日'], ['month', '本月'], ['all', '全部']]" :key="p[0]"
                type="button" class="chip" :class="{ active: preset === p[0] }" @click="apply(p[0])">{{ p[1] }}</button>
      </span>
      <label class="field">开始 <input v-model="start" type="date" @input="preset = ''" /></label>
      <label class="field">结束 <input v-model="end" type="date" @input="preset = ''" /></label>
      <label class="field">格式
        <select v-model="format">
          <option value="txt">TXT（适合发给 AI）</option>
          <option value="md">Markdown</option>
          <option value="json">JSON（原始数据）</option>
          <option value="csv">CSV（每日关键数据）</option>
        </select>
      </label>
      <button class="btn" type="button" :disabled="!valid || busy" @click="run">{{ busy ? '导出中…' : '导出' }}</button>
    </div>
    <p v-if="start && end && start > end" class="alert">开始日期晚于结束日期</p>
    <p v-if="error" class="alert">{{ error }}</p>
    <p v-if="hint" class="hint">
      {{ hint }}
      <button v-if="savedPath && canOpenFolder()" class="btn btn-ghost" type="button" @click="openFolder(savedPath)">打开所在文件夹</button>
    </p>
    <p class="muted small">每日总结永久保存，区间可以早于日线保留天数；一次最多 5 年。</p>
  </section>
</template>

<style scoped>
.range-export .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.presets { display: flex; gap: 6px; flex-wrap: wrap; }
.chip.active { border-color: rgba(232, 184, 109, 0.6); background: rgba(232, 184, 109, 0.12); }
</style>
