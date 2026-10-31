<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { CaptureStatus, ManualResult } from '@/api/types'
import { formatScore, scoreColor } from '@/utils/format'
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = withDefaults(defineProps<{ compact?: boolean }>(), { compact: false })
const status = ref<CaptureStatus | null>(null)
const result = ref<ManualResult | null>(null)
const error = ref('')
const fallbackHint = ref('')
const busy = ref(false)
const note = ref('')
const warming = ref(false)
let timer: number | undefined
let warmTimer: number | undefined

function stopPoll() {
  if (timer) window.clearInterval(timer)
  timer = undefined
}

async function poll() {
  try {
    const s = await api.captureStatus()
    status.value = s
    if (!s.running) {
      stopPoll()
      busy.value = false
      if (s.phase === 'done') await loadLatest()
      else if (s.phase === 'error') error.value = s.message
    }
  } catch (e) {
    stopPoll()
    busy.value = false
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function capture(fallback = false) {
  error.value = ''
  fallbackHint.value = ''
  note.value = ''
  busy.value = true
  try {
    const r = await api.startCapture(fallback)
    if ('result' in r) {
      result.value = r
      busy.value = false
      return
    }
    status.value = r
    stopPoll()
    timer = window.setInterval(poll, 800)
  } catch (e) {
    busy.value = false
    if (e instanceof ApiError && e.status === 409) fallbackHint.value = e.message
    else error.value = e instanceof Error ? e.message : String(e)
  }
}

async function cancel() {
  await api.cancelCapture().catch(() => undefined)
}

async function loadLatest(date?: string) {
  try {
    result.value = await api.manualSnapshot(date)
  } catch {
    // 没采集过
  }
}

async function exportInput() {
  try {
    const file = await api.aiInput('txt', undefined, 'manual')
    const url = URL.createObjectURL(file.blob)
    const a = document.createElement('a')
    a.href = url
    a.download = file.filename
    a.click()
    URL.revokeObjectURL(url)
    note.value = `已导出 ${file.filename}`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function rank() {
  note.value = '正在用快照生成 AI 排名…'
  try {
    await api.generateAiRanking({ snapshot: 'manual' })
    note.value = 'AI 排名已生成，到「AI 排名」页查看'
  } catch (e) {
    note.value = ''
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function pct(done: number, total: number) {
  return total ? Math.round((done / total) * 100) : 0
}

onMounted(async () => {
  const checkWarm = () =>
    api.backtestWarm().then((w) => {
      warming.value = w.running
      if (w.running) warmTimer = window.setTimeout(checkWarm, 3000)
    }).catch(() => undefined)
  checkWarm()
  await loadLatest()
  const s = await api.captureStatus().catch(() => null)
  if (s?.running) {
    status.value = s
    busy.value = true
    timer = window.setInterval(poll, 800)
  }
})
onBeforeUnmount(() => {
  stopPoll()
  if (warmTimer) window.clearTimeout(warmTimer)
})
</script>

<template>
  <section class="card manual-capture">
    <div class="card-head">
      <h2>手动采集当日行情</h2>
      <div class="actions">
        <button class="btn big" :disabled="busy" @click="capture(false)">采集当前行情并选前100</button>
        <span v-if="warming" class="muted">回测预热中…</span>
        <button v-if="busy" class="btn btn-ghost" @click="cancel">取消</button>
      </div>
    </div>
    <p class="muted">盘中点一下：从数据源（新浪优先）拉全部 A 股（不含科创板）实时快照，成交量/额/换手按已过交易时间换算成全天估算，和样本池做形态对比，取前 100。快照是临时数据，不进样本、不进回测，收盘同步后被正式日线替换。</p>
    <div v-if="busy && status" class="progress">
      <div class="bar"><span :style="{ width: pct(status.done, status.total) + '%' }"></span></div>
      <span>{{ status.message }} {{ status.total ? `${status.done}/${status.total}` : '' }}</span>
    </div>
    <p v-if="fallbackHint" class="warn">
      {{ fallbackHint }}
      <button class="btn btn-ghost" @click="capture(true)">改用上一交易日</button>
    </p>
    <p v-if="error" class="error">{{ error }}</p>
    <template v-if="result">
      <div class="meta">
        <strong class="tag">{{ result.kind === 'daily' ? '上一交易日正式日线 ' + result.date : result.label }}</strong>
        <span v-if="result.volume_estimated">已过 {{ result.elapsed_minutes }} 分钟，量能按完成比例 {{ (result.volume_fraction * 100).toFixed(1) }}% 换算</span>
        <span>{{ result.count }} 只 · 样本池 {{ result.result.pool_size }}</span>
        <span v-if="result.result.elapsed_seconds">耗时 {{ result.result.elapsed_seconds.toFixed(1) }} 秒</span>
        <span v-if="result.superseded" class="warn">当天正式日线已同步，此快照仅供回看</span>
        <select v-if="result.times && result.times.length > 1" disabled :title="'当天采集时间'">
          <option v-for="t in result.times" :key="t.taken_at">{{ t.taken_at.slice(11, 16) }}</option>
        </select>
      </div>
      <div class="actions">
        <button class="btn btn-ghost" :disabled="result.kind === 'daily'" @click="exportInput">导出 AI 输入（快照）</button>
        <button class="btn btn-ghost" :disabled="result.kind === 'daily'" @click="rank">用快照生成 AI 排名</button>
        <span class="muted">{{ note }}</span>
      </div>
      <div class="table-wrap" :class="{ short: props.compact }">
        <table>
          <thead>
            <tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>最高相似</th><th>平均</th><th>样本</th><th>综合分</th><th>等级</th><th>预测分</th><th>涨幅</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, i) in result.result.items" :key="item.code">
              <td>{{ i + 1 }}</td>
              <td>{{ item.code }}</td>
              <td>{{ item.name }} <span v-if="item.is_limit_up" class="tag up">涨停</span></td>
              <td>{{ item.board_name ?? '-' }}</td>
              <td :style="{ color: scoreColor(item.max_similarity) }">{{ formatScore(item.max_similarity) }}</td>
              <td>{{ formatScore(item.avg_similarity) }}</td>
              <td>{{ item.sample_count }}</td>
              <td>{{ item.composite_score == null ? '-' : item.composite_score.toFixed(1) }}</td>
              <td>{{ item.level ?? '-' }}</td>
              <td>{{ item.prediction_score == null ? '-' : item.prediction_score.toFixed(1) }}</td>
              <td>{{ item.change_percent == null ? '-' : item.change_percent.toFixed(2) + '%' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.manual-capture .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
.big { font-size: 15px; padding: 10px 18px; font-weight: 600; }
.progress { display: flex; gap: 12px; align-items: center; margin: 8px 0; }
.progress .bar { flex: 1; height: 8px; background: var(--border, #ddd); border-radius: 4px; overflow: hidden; }
.progress .bar span { display: block; height: 100%; background: var(--accent, #c0392b); transition: width .3s; }
.meta { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
.tag { padding: 2px 8px; border-radius: 4px; background: rgba(192, 57, 43, .12); }
.tag.up { color: #c0392b; font-size: 12px; }
.warn { color: #b9770e; }
.error { color: #c0392b; }
.table-wrap { max-height: 560px; overflow: auto; }
.table-wrap.short { max-height: 360px; }
</style>
