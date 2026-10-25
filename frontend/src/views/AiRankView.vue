<script setup lang="ts">
import type { DailyRankPick, MemoryDayFile } from '@/api/types'
import { dailyRank, prepareFeed, refreshDailyRankNow, releaseDailyRank, retainDailyRank, runPicksCheck, startRanking } from '@/stores/dailyRank'
import { aiMemory, loadAiMemory, releaseAiMemory, retainAiMemory, runAiMemoryNow } from '@/stores/aiMemory'
import { aiReady } from '@/stores/session'
import { formatBytes, formatDateTime, formatGap, formatPct, formatPrice, formatScore, pctClass, spendShort, spendWord } from '@/utils/format'
import { windowModeLabel } from '@/utils/labels'
import { onMounted, onUnmounted, computed } from 'vue'

const board = computed(() => dailyRank.state?.board ?? null)
const result = computed(() => board.value?.result ?? null)
const runner = computed(() => dailyRank.runner ?? null)
const busy = computed(() => dailyRank.ranking || dailyRank.pendingRank || runner.value?.running === true)
/** 钱这两笔记在谁头上：内存槽位说的是哪一天就抄它；重启之后槽位空了，抄库里那一趟留下的账。 */
const spend = computed(() => {
  const last = dailyRank.runner?.last
  if (last?.trade_date && last.trade_date === board.value?.trade_date) return last
  const stored = board.value?.result
  if (stored && (stored.request_sent !== undefined || stored.billed !== undefined)) {
    return { request_sent: stored.request_sent, billed: stored.billed }
  }
  return null
})
/** 输入在就能排；这一天已经排出过就不让再按——再按一次就是再花一次钱。失败的那一份留着输入，可以重试。 */
const feedReady = computed(() => (board.value?.feed_rows ?? 0) > 0)
const ranked = computed(() => board.value?.status === 'done')

const statusWords: Record<string, string> = {
  prepared: '输入已备好',
  done: '名次已排出',
  failed: '这一趟失败',
  running: '正在排名',
}

function statusWord(status: string | null | undefined): string {
  if (!status) return '没备过'
  return statusWords[status] ?? '状态未知'
}

/** 「上一趟」：内存槽位有就抄它；重启之后槽位空了，退回库里最近那一份固定榜自己的账。 */
const lastRun = computed(() => {
  const last = dailyRank.runner?.last
  if (last?.trade_date) {
    return { date: last.trade_date, word: last.ok ? '成功' : '失败', spend: spendShort(last) }
  }
  const row = board.value
  if (row && (row.status === 'done' || row.status === 'failed')) {
    return { date: row.trade_date, word: row.status === 'done' ? '成功' : '失败', spend: spend.value ? spendShort(spend.value) : '—' }
  }
  return null
})

const indicatorLabels: Record<string, string> = {
  similarity_score: '最高相似度',
  pattern_match_rate: '高相似占比',
  technical_score: '技术分',
  volume_score: '成交量',
  capital_score: '资金',
  history_limit_up_rate: '历史涨停率',
  risk_score: '风险',
}

const gates = computed(() => {
  const row = board.value
  if (!row) return []
  return [
    { label: '池外代码', value: row.dropped_codes_count },
    { label: '缺分/越界', value: row.rejected_count },
    { label: '超出前 20', value: row.picks_beyond_limit },
    { label: '数字追不回', value: row.unverified_citations },
  ]
})

function indicatorText(pick: DailyRankPick): string {
  const scores = pick.indicator_scores || {}
  return Object.entries(indicatorLabels)
    .map(([key, label]) => `${label} ${scores[key] == null ? '—' : formatScore(scores[key])}`)
    .join(' · ')
}

/** 补对账改的是同一本账：快照那块的「最新对账」也当场跟上。 */
async function checkNow(): Promise<void> {
  await runPicksCheck()
  void loadAiMemory()
}

function onVisibility(): void {
  if (document.visibilityState === 'visible') {
    refreshDailyRankNow()
    void loadAiMemory()
  }
}

/** 两份目录按日子并排：文件名就是日子，哪一天只落了一份，另一格画空。 */
const snapshotRows = computed(() => {
  const merged = new Map<string, { memory?: MemoryDayFile; failure?: MemoryDayFile }>()
  for (const item of aiMemory.days?.model_memory.days ?? []) {
    merged.set(item.date, { ...merged.get(item.date), memory: item })
  }
  for (const item of aiMemory.days?.failure_logs.days ?? []) {
    merged.set(item.date, { ...merged.get(item.date), failure: item })
  }
  return [...merged.entries()]
    .map(([date, row]) => ({ date, memory: row.memory, failure: row.failure }))
    .sort((a, b) => (a.date < b.date ? 1 : -1))
})

const runLegs = computed(() => {
  const run = aiMemory.lastRun
  if (!run) return []
  const legs: { label: string; value: string }[] = []
  if (run.reconcile) {
    legs.push({ label: '本轮定判', value: `${run.reconcile.judged} 条（中 ${run.reconcile.hits} / 不中 ${run.reconcile.misses} / 还待验证 ${run.reconcile.still_pending}）` })
  } else {
    legs.push({ label: '本轮定判', value: '这一趟没做成' })
  }
  legs.push({
    label: `落 ${run.trade_date}`,
    value: run.export_today
      ? `样本 ${run.export_today.memory_samples} 条 · 误判 ${run.export_today.failure_cases} 条 · ${formatBytes(run.export_today.memory_bytes + run.export_today.failure_bytes)}`
      : '没落盘',
  })
  const previous = run.export_previous
  legs.push({
    label: '回填上一交易日',
    value: previous ? `${previous.trade_date}：样本 ${previous.memory_samples} 条 · 误判 ${previous.failure_cases} 条` : '库里没有更早的行情日',
  })
  return legs
})

onMounted(() => {
  retainDailyRank()
  retainAiMemory()
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  releaseDailyRank()
  releaseAiMemory()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>每日 AI 固定榜</h1>
      <p class="lede">
        备输入 {{ dailyRank.history?.feed_limit ?? 100 }} 行不花钱，排出前 {{ dailyRank.history?.pick_count ?? 20 }} 名才敲一次外部模型。
      </p>
    </div>
    <div class="toolbar">
      <span v-if="board" class="muted nowrap">交易日 {{ board.trade_date || '—' }} · {{ board.metric_label }}</span>
      <button class="btn btn-ghost" type="button" @click="refreshDailyRankNow">刷新</button>
    </div>
  </header>

  <p v-if="dailyRank.error" class="alert">{{ dailyRank.error }}</p>
  <p v-else-if="dailyRank.note" class="hint">{{ dailyRank.note }}</p>
  <p v-if="dailyRank.actionError" class="alert">{{ dailyRank.actionError }}</p>

  <section class="card">
    <div class="card-head">
      <h2>两段账</h2>
      <div class="toolbar">
        <button class="btn btn-ghost" type="button" :disabled="dailyRank.preparing || busy" @click="prepareFeed">
          {{ dailyRank.preparing ? '备输入中…' : '备好输入（不花钱）' }}
        </button>
        <button class="btn" type="button" :disabled="!aiReady() || !feedReady || ranked || busy" @click="startRanking">
          {{ busy ? '排名中…' : ranked ? '这一天已排出' : `排出前 ${dailyRank.history?.pick_count ?? 20} 名（花钱）` }}
        </button>
      </div>
    </div>

    <template v-if="busy">
      <div class="progress running"><i :style="{ '--w': '100%' }" /></div>
      <p class="hint">
        已跑 {{ runner?.running_seconds ?? 0 }} 秒 · 正文 {{ runner?.answer_chars ?? 0 }} 字 · 思考 {{ runner?.thinking_chars ?? 0 }} 字
      </p>
    </template>

    <div v-if="board" class="stats" style="margin-top: 8px">
      <article class="card stat">
        <div class="stat-label">状态</div>
        <div class="stat-value" style="font-size: 20px">{{ statusWord(board.status) }}</div>
        <div class="stat-foot">交易日 {{ board.trade_date || '—' }} · 形态截至 {{ board.base_date || '—' }}</div>
      </article>
      <article class="card stat">
        <div class="stat-label">榜 / 喂给模型</div>
        <div class="stat-value">{{ board.board_rows }} / {{ board.feed_rows }}</div>
        <div class="stat-foot">{{ board.metric_label }} · {{ windowModeLabel(board.window_mode) }}</div>
      </article>
      <article class="card stat">
        <div class="stat-label">名次</div>
        <div class="stat-value">{{ board.picks_count }}</div>
        <div class="stat-foot">{{ board.model || '还没有模型结论' }}</div>
      </article>
      <article class="card stat">
        <div class="stat-label">这次花了</div>
        <div class="stat-value stat-text">{{ board.cost_text || '—' }}</div>
        <div class="stat-foot">{{ board.latency_seconds == null ? '没有耗时记录' : `${board.latency_seconds} 秒` }}</div>
      </article>
    </div>
    <div v-else class="empty">还没有备过任何一天的输入</div>

    <div v-if="board && (board.status === 'failed' || board.error)" class="alert" style="margin-top: 8px">
      {{ board.error || '这一趟失败了' }}
      <span v-if="spend && board.status === 'failed'" class="muted"> · {{ spendWord(spend) }}</span>
    </div>

    <div class="readonly" style="margin-top: 8px">
      <div class="two">
        <div>
          <div><span class="muted">输入定格于</span> <b>{{ formatDateTime(board?.prepared_at) }}</b></div>
          <div><span class="muted">排名开始</span> <b>{{ formatDateTime(board?.started_at) }}</b></div>
        </div>
        <div>
          <div><span class="muted">排名结束</span> <b>{{ formatDateTime(board?.finished_at) }}</b></div>
          <div><span class="muted">上一趟</span> <b>{{ lastRun ? `${lastRun.date || '—'} · ${lastRun.word} · ${lastRun.spend}` : '没有' }}</b></div>
        </div>
      </div>
    </div>
  </section>

  <section v-if="result && result.picks.length" class="card">
    <div class="card-head">
      <h2>名次</h2>
      <div class="toolbar">
        <span class="muted">{{ result.picks.length }} 条 · 输入 {{ result.feed_rows }} 行</span>
      </div>
    </div>
    <div class="table-wrap" style="max-height: calc(100vh - 260px)">
      <table>
        <thead>
          <tr>
            <th>名次</th>
            <th>股票</th>
            <th class="right">AI 自报分</th>
            <th class="right">综合评分</th>
            <th class="right">差</th>
            <th class="right">输入位次</th>
            <th>理由</th>
            <th>反证</th>
            <th>指标</th>
            <th>闸门</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="pick in result.picks" :key="pick.stock_code">
            <td class="rank" :class="{ top: pick.rank <= 3 }">{{ pick.rank }}</td>
            <td>
              <div>{{ pick.stock_name }}</div>
              <div class="code">{{ pick.stock_code }}</div>
              <div class="hint">{{ pick.evidence.board }} · {{ pick.evidence.at_limit ? '已涨停' : '未涨停' }} · 现价 {{ formatPrice(pick.evidence.last_price) }} <span :class="pctClass(pick.evidence.change_percent)">{{ formatPct(pick.evidence.change_percent) }}</span></div>
            </td>
            <td class="right">{{ formatScore(pick.score) }}</td>
            <td class="right">
              <b>{{ pick.formula.has_signal ? formatScore(pick.formula.composite_score) : '—' }}</b>
              <div class="hint">{{ pick.formula.has_signal ? pick.formula.level : '' }}</div>
            </td>
            <td class="right">{{ pick.formula.model_gap == null ? '—' : formatScore(pick.formula.model_gap) }}</td>
            <td class="right">{{ pick.feed_rank ?? '—' }}</td>
            <td class="wrap">{{ pick.reason || '—' }}</td>
            <td class="wrap">{{ pick.counter_evidence || '—' }}</td>
            <td class="hint wrap">{{ indicatorText(pick) }}</td>
            <td>
              <span v-if="pick.unverified_citation" class="tag type-weak">数字追不回</span>
              <span v-else class="tag">对得上</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="chips" style="margin-top: 10px">
      <span v-for="item in gates" :key="item.label" class="chip">{{ item.label }} {{ item.value }}</span>
      <span v-if="result.dropped_codes.length" class="chip">池外 {{ result.dropped_codes.join('、') }}</span>
      <span v-if="result.rejected_codes.length" class="chip">剔掉 {{ result.rejected_codes.join('、') }}</span>
      <span v-if="result.unverified_codes.length" class="chip">盖章 {{ result.unverified_codes.join('、') }}</span>
      <span v-if="result.text_truncated" class="chip">理由被截</span>
      <span v-if="result.criteria_truncated" class="chip">标准被截</span>
    </div>
    <div v-if="result.criteria" style="margin-top: 10px">
      <b>它的判断标准</b>
      <p class="hint" style="white-space: pre-wrap">{{ result.criteria }}</p>
    </div>
    <p class="hint" style="margin-top: 10px">{{ board?.formula }}</p>
    <p class="hint">{{ board?.disclaimer }}</p>
    <pre v-if="result.raw_excerpt" class="raw">{{ result.raw_excerpt }}</pre>
  </section>

  <section v-if="board && board.feed.length" class="card">
    <div class="card-head">
      <h2>喂给模型的输入</h2>
      <span class="muted nowrap">{{ board.feed.length }} 行 · 定格于 {{ formatDateTime(board.prepared_at) }}</span>
    </div>
    <div class="table-wrap" style="max-height: 420px">
      <table>
        <thead>
          <tr>
            <th>位次</th>
            <th>股票</th>
            <th>板块</th>
            <th>涨停</th>
            <th class="right">现价</th>
            <th class="right">涨幅</th>
            <th class="right">距涨停</th>
            <th class="right">最高相似</th>
            <th class="right">平均</th>
            <th class="right">高相似/样本</th>
            <th>档位</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in board.feed" :key="row.stock_code">
            <td class="rank">{{ row.feed_rank }}</td>
            <td>
              <div>{{ row.stock_name }}</div>
              <div class="code">{{ row.stock_code }}</div>
            </td>
            <td>{{ row.board }}</td>
            <td><span class="tag" :class="row.at_limit ? 'tag-hot' : 'type-weak'">{{ row.at_limit ? '已涨停' : '未涨停' }}</span></td>
            <td class="right">{{ formatPrice(row.last_price) }}</td>
            <td class="right">{{ formatPct(row.change_percent) }}</td>
            <td class="right">{{ formatGap(row.to_limit_percent) }}</td>
            <td class="right">{{ formatScore(row.max_similarity) }}</td>
            <td class="right">{{ formatScore(row.avg_similarity) }}</td>
            <td class="right">{{ row.high_sim_count ?? '—' }}/{{ row.sample_count ?? '—' }}</td>
            <td><span class="tag">{{ row.band }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>它的历史账</h2>
      <div class="toolbar">
        <span v-if="dailyRank.summary" class="muted">定判 {{ dailyRank.summary.check.settled }} / 共 {{ dailyRank.summary.check.total }} 条 · 命中 {{ dailyRank.summary.check.hits }}</span>
        <button class="btn btn-ghost" type="button" :disabled="dailyRank.checking" @click="checkNow">
          {{ dailyRank.checking ? '对账中…' : '当场补一次对账' }}
        </button>
      </div>
    </div>
    <div v-if="dailyRank.lastCheck" class="chips" style="margin-bottom: 10px">
      <span class="chip">本轮定判 {{ dailyRank.lastCheck.judged }}</span>
      <span class="chip">中 {{ dailyRank.lastCheck.hits }}</span>
      <span class="chip">不中 {{ dailyRank.lastCheck.misses }}</span>
      <span class="chip">还待验证 {{ dailyRank.lastCheck.still_pending }}</span>
    </div>
    <div v-if="dailyRank.summary" class="table-wrap" style="max-height: 360px">
      <table>
        <thead>
          <tr>
            <th>判据日</th>
            <th class="right">条数</th>
            <th class="right">命中</th>
            <th class="right">没中</th>
            <th class="right">待验证</th>
            <th class="right">命中率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in dailyRank.summary.days" :key="item.judge_date">
            <td>{{ item.judge_date }}</td>
            <td class="right">{{ item.rows }}</td>
            <td class="right">{{ item.hits }}</td>
            <td class="right">{{ item.misses }}</td>
            <td class="right">{{ item.pending }}</td>
            <td class="right">{{ item.hit_rate_percent == null ? '—' : formatScore(item.hit_rate_percent) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="hint" style="margin-top: 8px">{{ dailyRank.summary.verdict_rule }}</p>
      <p class="hint">{{ dailyRank.summary.disclaimer }}</p>
    </div>
    <div v-else class="empty">还没有对它说过的那些话对过账</div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>每日 JSON 快照</h2>
      <div class="toolbar">
        <span v-if="aiMemory.days" class="muted">
          样本 {{ aiMemory.days.model_memory.day_count }} 天 · 误判 {{ aiMemory.days.failure_logs.day_count }} 天 · {{ formatBytes(aiMemory.days.model_memory.total_bytes + aiMemory.days.failure_logs.total_bytes) }}
        </span>
        <button class="btn btn-ghost" type="button" :disabled="aiMemory.running" @click="runAiMemoryNow">
          {{ aiMemory.running ? '这一趟正在跑…' : '跑一遍对账与快照' }}
        </button>
      </div>
    </div>

    <p v-if="aiMemory.error" class="alert">{{ aiMemory.error }}</p>
    <p v-else-if="aiMemory.note" class="hint">{{ aiMemory.note }}</p>

    <div v-if="runLegs.length" class="chips" style="margin-bottom: 10px">
      <span v-for="leg in runLegs" :key="leg.label" class="chip">{{ leg.label }} {{ leg.value }}</span>
    </div>
    <div v-if="aiMemory.lastRun?.errors.length" class="alert" style="margin-bottom: 10px">
      <div v-for="(item, index) in aiMemory.lastRun.errors" :key="index">{{ item }}</div>
    </div>

    <div v-if="aiMemory.summary" class="table-wrap" style="max-height: 360px">
      <table>
        <thead>
          <tr>
            <th>日子</th>
            <th>模型记忆</th>
            <th class="right">大小</th>
            <th>误判库</th>
            <th class="right">大小</th>
            <th class="right">落盘时刻</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in snapshotRows" :key="row.date">
            <td>{{ row.date }}</td>
            <td><span class="tag" :class="row.memory ? 'tag-hot' : 'type-weak'">{{ row.memory ? '有' : '没有' }}</span></td>
            <td class="right">{{ row.memory ? formatBytes(row.memory.bytes) : '—' }}</td>
            <td><span class="tag" :class="row.failure ? 'tag-hot' : 'type-weak'">{{ row.failure ? '有' : '没有' }}</span></td>
            <td class="right">{{ row.failure ? formatBytes(row.failure.bytes) : '—' }}</td>
            <td class="right">{{ formatDateTime(row.memory?.written_at || row.failure?.written_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="readonly" style="margin-top: 10px">
        <div class="two">
          <div>
            <div><span class="muted">模型记忆目录</span> <b class="mono">{{ aiMemory.summary.export.model_memory.dir }}</b></div>
            <div><span class="muted">误判库目录</span> <b class="mono">{{ aiMemory.summary.export.failure_logs.dir }}</b></div>
          </div>
          <div>
            <div><span class="muted">库里最新行情日</span> <b>{{ aiMemory.summary.latest_quote_date || '还没有行情' }}</b></div>
            <div><span class="muted">最新对账</span> <b>{{ formatDateTime(aiMemory.summary.check.check.last_checked_at) }}</b></div>
          </div>
        </div>
      </div>
      <div v-for="(gap, index) in aiMemory.summary.field_gaps" :key="index" class="hint">{{ gap }}</div>
      <p class="hint">{{ aiMemory.summary.disclaimer }}</p>
    </div>
    <div v-else class="empty">快照读数一次都没拿到过</div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>备过的日子</h2>
      <span class="muted">留 {{ dailyRank.history?.retention_days ?? 90 }} 天</span>
    </div>
    <div v-if="!dailyRank.history?.items.length" class="empty">库里还没有固定榜</div>
    <div v-else class="table-wrap" style="max-height: 360px">
      <table>
        <thead>
          <tr>
            <th>交易日</th>
            <th>状态</th>
            <th>口径</th>
            <th>模型</th>
            <th class="right">输入</th>
            <th class="right">名次</th>
            <th class="right">缺分/越界</th>
            <th class="right">数字追不回</th>
            <th class="right">耗时</th>
            <th>花了</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in dailyRank.history.items" :key="`${item.trade_date}-${item.status}`">
            <td>{{ item.trade_date || '—' }}</td>
            <td><span class="tag" :class="item.status === 'done' ? 'tag-hot' : item.status === 'failed' ? 'type-weak' : 'type-normal'">{{ statusWord(item.status) }}</span></td>
            <td>{{ item.metric_label }}</td>
            <td class="code">{{ item.model || '—' }}</td>
            <td class="right">{{ item.feed_rows }}</td>
            <td class="right">{{ item.picks_count }}</td>
            <td class="right">{{ item.rejected_count }}</td>
            <td class="right">{{ item.unverified_citations }}</td>
            <td class="right">{{ item.latency_seconds == null ? '—' : `${item.latency_seconds} 秒` }}</td>
            <td class="wrap">{{ item.cost_text || '—' }}<span v-if="item.error" class="hint"> · {{ item.error }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
