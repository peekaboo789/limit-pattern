<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import { saveExportedText } from '@/utils/saveFile'
import { ref } from 'vue'

/** 导出前 100 给外部 AI 分析：TXT / MD。date 为空时用最新数据（盘中为实时快照）。 */
const props = defineProps<{ date?: string }>()
const busy = ref(false)
const hint = ref('')

async function run(format: 'txt' | 'md') {
  if (busy.value) return
  busy.value = true
  hint.value = ''
  try {
    const file = await api.aiInput(format, props.date || undefined)
    const mode = await saveExportedText(file.filename, file.text, file.blob)
    hint.value =
      mode === 'cancel' ? '已取消保存' : `已${mode === 'dialog' ? '保存' : '下载'} ${file.filename}（${file.text.length} 字），发给外部 AI 后把返回的 JSON 粘到「导入外部 AI 结果」`
  } catch (reason) {
    hint.value = reason instanceof ApiError ? reason.detail : '导出失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <span class="export-action">
    <button class="btn btn-ghost" type="button" :disabled="busy" title="相似度前 100 只 + 市场环境 + 评分框架，发给任意外部 AI" @click="run('txt')">
      {{ busy ? '导出中…' : '导出前 100 给 AI 分析' }}
    </button>
    <button class="btn btn-ghost" type="button" :disabled="busy" @click="run('md')">.md</button>
    <span v-if="hint" class="hint">{{ hint }}</span>
  </span>
</template>
