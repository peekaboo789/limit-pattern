<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { DataCleanupJob, DataCleanupPreview } from '@/api/types'
import { computed, onMounted, onUnmounted, ref } from 'vue'

const days = ref(60)
const autoEnabled = ref(false)
const preview = ref<DataCleanupPreview | null>(null)
const loading = ref(false)
const running = ref(false)
const savingAuto = ref(false)
const confirmed = ref(false)
const message = ref('')
const error = ref('')
const result = ref<Record<string, unknown> | null>(null)
const job = ref<DataCleanupJob | null>(null)
const jobProgress = ref(0)
let pollTimer: number | null = null

const daysValid = computed(() => Number.isInteger(days.value) && days.value >= 30 && days.value <= 365)
const calcProtectionNeeded = computed(() => (preview.value?.calc_protected_archive ?? 0) > 0)

function bytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

async function loadSettings() {
  const settings = await api.settings()
  days.value = settings.data_cleanup_days ?? 60
  autoEnabled.value = settings.data_cleanup_auto_enabled === true
}

async function loadPreview() {
  loading.value = true
  error.value = ''
  preview.value = null
  try {
    preview.value = await api.dataCleanupPreview(days.value)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '预览失败'
  } finally {
    loading.value = false
  }
}

async function saveAuto() {
  if (!daysValid.value) {
    error.value = '删除天数需为 30–365 的整数'
    return
  }
  savingAuto.value = true
  error.value = ''
  message.value = ''
  try {
    await api.saveSettings({ data_cleanup_days: days.value, data_cleanup_auto_enabled: autoEnabled.value })
    message.value = '自动删除设置已保存'
    await loadPreview()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '保存失败'
  } finally {
    savingAuto.value = false
  }
}

async function execute() {
  if (!confirmed.value) {
    error.value = '请先勾选“我已知道这是不可恢复的删除操作”'
    return
  }
  running.value = true
  job.value = null
  jobProgress.value = 0
  error.value = ''
  result.value = null
  message.value = ''
  try {
    const started = await api.dataCleanupExecute(days.value)
    job.value = started
    jobProgress.value = started.progress ?? 0
    confirmed.value = false
    await pollJob(started.job_id)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '删除失败'
  } finally {
    running.value = false
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

async function pollJob(jobId: string) {
  stopPolling()
  try {
    const status = await api.dataCleanupJob(jobId)
    job.value = status
    jobProgress.value = status.progress ?? jobProgress.value
    if (!status.running) {
      running.value = false
      if (status.status === 'done') {
        result.value = status.result ?? null
        message.value = '删除完成'
      } else {
        error.value = status.error || status.message || '删除失败，可重试'
      }
      await loadPreview()
      return
    }
    pollTimer = window.setInterval(async () => {
      try {
        const next = await api.dataCleanupJob(jobId)
        job.value = next
        jobProgress.value = next.progress ?? jobProgress.value
        if (!next.running) {
          stopPolling()
          running.value = false
          if (next.status === 'done') {
            result.value = next.result ?? null
            message.value = '删除完成'
          } else {
            error.value = next.error || next.message || '删除失败，可重试'
          }
          await loadPreview()
        }
      } catch (reason) {
        stopPolling()
        running.value = false
        error.value = reason instanceof ApiError ? reason.detail : '轮询任务状态失败，可重试'
      }
    }, 2000)
  } catch (reason) {
    running.value = false
    error.value = reason instanceof ApiError ? reason.detail : '获取任务状态失败，可重试'
  }
}

onMounted(async () => {
  try {
    await loadSettings()
    await loadPreview()
  } catch {
    // 设置页主流程会显示加载错误
  }
})

onUnmounted(stopPolling)
</script>

<template>
  <section class="card">
    <div class="card-head"><h2>删除旧数据</h2></div>
    <p class="hint">
      手动删除 N 个自然日之前累积的数据；执行前自动备份数据库，完成后 VACUUM 回收空间。只做网页操作，不接入企业微信/个人微信命令。
    </p>
    <label class="field" style="margin-top: 12px">
      <span>删除多少天前的数据（30–365，自然日，默认 60）</span>
      <input v-model.number="days" type="number" min="30" max="365" step="1" />
      <span class="hint" :class="{ invalid: !daysValid }">删除线为今天往前 {{ days }} 天；恰好 {{ days }} 天前这一天的数据会保留。只允许 30–365 的整数。</span>
    </label>
    <label class="check">
      <input v-model="autoEnabled" type="checkbox" /> 每天 03:10 自动删除 N 天前数据
    </label>
    <div class="actions">
      <button class="btn btn-ghost" type="button" :disabled="loading || savingAuto" @click="loadPreview">刷新预览</button>
      <button class="btn btn-ghost" type="button" :disabled="savingAuto" @click="saveAuto">{{ savingAuto ? '保存中…' : '保存自动删除设置' }}</button>
      <button class="btn danger" type="button" :disabled="running || !daysValid || loading" @click="execute">{{ running ? '删除中…' : '执行删除' }}</button>
    </div>
    <div v-if="job" class="job-status">
      <p class="hint">任务 {{ job.job_id }}：{{ job.message }}（{{ Math.round(jobProgress) }}%）</p>
      <div class="progress-track"><div class="progress-fill" :style="{ width: `${Math.min(100, jobProgress)}%` }"></div></div>
      <p class="hint">已用 {{ job.elapsed_seconds.toFixed(1) }} 秒</p>
    </div>
    <p v-if="error" class="hint" style="color: #dc2626">{{ error }}</p>
    <p v-if="message" class="hint" style="color: #16a34a">{{ message }}</p>
    <label class="check danger-check">
      <input v-model="confirmed" type="checkbox" /> 我已知道这是不可恢复的删除操作
    </label>
    <div v-if="preview" class="preview">
      <p class="hint">
        今天 {{ preview.today }}（Asia/Shanghai），删除 {{ preview.cutoff }} 之前的数据。计算所需历史线 {{ preview.calc_cutoff || '无行情' }}，
        受保护转存 {{ preview.calc_protected_archive }} 条。
      </p>
      <p v-if="calcProtectionNeeded" class="hint warning">本组天数会触及形态计算所需历史窗口；程序会先把这段正式日线转存到 calc_bars，position_60 等计算不受影响。</p>
      <p v-else class="hint">{{ preview.calc_note }}</p>
      <p class="hint">当前预览为手动删除口径：允许删 60 天前的 AI 前 20 榜单。若开启每天自动删除，自动任务仍不删 AI 前 20 榜单和每日总结表。</p>
      <div class="grid">
        <div>
          <h3>数据库将删除</h3>
          <ul>
            <li v-for="row in preview.tables" :key="row.label">{{ row.label }}：{{ row.count }} 条</li>
          </ul>
        </div>
        <div>
          <h3>文件将删除</h3>
          <ul>
            <li v-for="row in preview.files" :key="row.label">{{ row.label }}：{{ row.count }} 个 / {{ bytes(row.size) }}</li>
          </ul>
          <p class="hint">共 {{ preview.total_files }} 个文件，{{ bytes(preview.total_file_size) }}</p>
        </div>
      </div>
      <p class="hint">永远不删：{{ preview.protected.join('、') }}</p>
    </div>
    <div v-if="result" class="preview">
      <h3>删除完成</h3>
      <pre>{{ JSON.stringify(result, null, 2) }}</pre>
    </div>
  </section>
</template>

<style scoped>
.actions { display: flex; gap: 8px; margin: 12px 0; flex-wrap: wrap; }
.check { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
.danger { background: #7f1d1d; border-color: #b91c1c; }
.danger-check { color: #fca5a5; }
.invalid { color: #f59e0b; }
.warning { color: #f59e0b; }
.preview { margin-top: 12px; padding: 10px; border: 1px solid #292f3d; border-radius: 8px; background: rgba(255, 255, 255, 0.02); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
h3 { font-size: 14px; margin: 10px 0 4px; }
ul { margin: 0; padding-left: 18px; }
li { color: var(--text); font-size: 13px; }
pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; color: var(--muted); }
  .job-status { margin-top: 8px; padding: 8px; border: 1px solid #292f3d; border-radius: 8px; background: rgba(255, 255, 255, 0.02); }
  .progress-track { height: 8px; margin: 8px 0; border-radius: 999px; background: #1f2937; overflow: hidden; }
  .progress-fill { height: 100%; background: #16a34a; transition: width 0.2s ease; }
</style>
