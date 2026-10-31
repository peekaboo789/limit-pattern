<script setup lang="ts">
import { capture, cancel, exportInput, rank, startWarmWatch, initCapture, state } from '@/stores/capture'
import { canOpenFolder, openFolder } from '@/utils/saveFile'
import { formatScore, scoreColor } from '@/utils/format'
import { computed, onMounted } from 'vue'
import { RouterLink } from 'vue-router'

const props = withDefaults(defineProps<{ compact?: boolean }>(), { compact: false })
const items = computed(() => {
  const list = state.result?.result.items ?? []
  return props.compact ? list.slice(0, 5) : list
})

function pct(done: number, total: number) {
  return total ? Math.round((done / total) * 100) : 0
}

onMounted(() => {
  initCapture()
  startWarmWatch()
})
</script>

<template>
  <section class="card manual-capture">
    <div class="card-head">
      <h2>手动采集当日行情</h2>
      <div class="actions">
        <button class="btn big" :disabled="state.busy" @click="capture(false)">采集当前行情并选前100</button>
        <span v-if="state.warming" class="muted">回测预热中…</span>
        <button v-if="state.busy" class="btn btn-ghost" @click="cancel">取消</button>
      </div>
    </div>
    <p class="muted">盘中点一下：从数据源（新浪优先）拉全部 A 股（不含科创板）实时快照，成交量/额/换手按已过交易时间换算成全天估算，和样本池做形态对比，取前 100。快照是临时数据，不进样本、不进回测，收盘同步后被正式日线替换。</p>
    <div v-if="state.busy && state.status" class="progress">
      <div class="bar"><span :style="{ width: pct(state.status.done, state.status.total) + '%' }"></span></div>
      <span>{{ state.status.message }} {{ state.status.total ? `${state.status.done}/${state.status.total}` : '' }}</span>
    </div>
    <p v-if="state.fallbackHint" class="warn">
      {{ state.fallbackHint }}
      <button class="btn btn-ghost" @click="capture(true)">改用上一交易日</button>
    </p>
    <p v-if="state.error" class="error">{{ state.error }}</p>
    <template v-if="state.result">
      <div class="meta">
        <strong class="tag">{{ state.result.kind === 'daily' ? '上一交易日正式日线 ' + state.result.date : state.result.label }}</strong>
        <span v-if="state.result.volume_estimated">已过 {{ state.result.elapsed_minutes }} 分钟，量能按完成比例 {{ (state.result.volume_fraction * 100).toFixed(1) }}% 换算</span>
        <span>{{ state.result.count }} 只 · 样本池 {{ state.result.result.pool_size }}</span>
        <span v-if="state.result.result.elapsed_seconds">耗时 {{ state.result.result.elapsed_seconds.toFixed(1) }} 秒</span>
        <span v-if="state.result.superseded" class="warn">当天正式日线已同步，此快照仅供回看</span>
        <select v-if="state.result.times && state.result.times.length > 1" disabled :title="'当天采集时间'">
          <option v-for="t in state.result.times" :key="t.taken_at">{{ t.taken_at.slice(11, 16) }}</option>
        </select>
      </div>
      <div class="actions">
        <button class="btn btn-ghost" :disabled="state.exporting" @click="exportInput('txt')">导出给AI分析</button>
        <button class="btn btn-ghost" :disabled="state.exporting" @click="exportInput('md')">.md</button>
        <button class="btn btn-ghost" :disabled="state.ranking" @click="rank">{{ state.ranking ? 'AI 榜单生成中…' : '生成AI榜单' }}</button>
        <RouterLink v-if="props.compact" class="btn btn-ghost" to="/similarity">查看完整前 100 →</RouterLink>
        <span class="muted">{{ state.note }}</span>
        <button v-if="state.savedPath && canOpenFolder()" class="btn btn-ghost" @click="openFolder(state.savedPath)">打开所在文件夹</button>
      </div>
      <div class="table-wrap" :class="{ short: props.compact }">
        <table>
          <thead>
            <tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>最高相似</th><th>平均</th><th>样本</th><th>综合分</th><th>等级</th><th>预测分</th><th>涨幅</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, i) in items" :key="item.code">
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
