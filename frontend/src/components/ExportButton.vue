<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { ExportParams } from '@/api/types'
import { saveExportedText } from '@/utils/saveFile'
import { computed, ref } from 'vue'

const props = defineProps<{
  params: ExportParams | null
}>()

const exporting = ref(false)
const hint = ref('')
const error = ref('')

const title = computed(() => {
  const params = props.params
  if (!params) return '请先选定日期'
  if (params.type === 'scan') return `导出 ${params.date} 扫描前 ${params.top ?? 50} 条`
  if (params.type === 'similarity') return `导出 ${params.code} 在 ${params.date} 的前 ${params.top ?? 20} 条`
  if (params.type === 'pool') return `导出 ${params.date} 涨停池`
  if (params.type === 'next_day_pool') return `导出 ${params.date} 次日观察池`
  if (params.type === 'backtest') return '导出回测结果'
  if (params.type === 'ai_ranking') return `导出 ${params.date} AI 前 20 榜单`
  return `导出 ${params.date} 每日总结`
})

async function onClick() {
  if (!props.params || exporting.value) return
  exporting.value = true
  hint.value = ''
  error.value = ''
  try {
    const file = await api.exportText(props.params)
    const mode = await saveExportedText(file.filename, file.text, file.blob)
    hint.value = mode === 'cancel' ? '已取消保存' : mode === 'dialog' ? `已保存 ${file.filename}` : `已下载 ${file.filename}`
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '导出失败'
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <span class="export-action">
    <button class="btn btn-ghost" type="button" :title="title" :disabled="!params || exporting" @click="onClick">
      {{ exporting ? '导出中…' : '导出 TXT' }}
    </button>
    <span v-if="error" class="hint">{{ error }}</span>
    <span v-else-if="hint" class="hint">{{ hint }}</span>
  </span>
</template>
