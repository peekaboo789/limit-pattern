<script setup lang="ts">
import type { AiPick, RealtimeRow } from '@/api/types'
import IntradayWatchPanel from '@/components/IntradayWatchPanel.vue'
import { aiReady } from '@/stores/session'
import { realtime, refreshRealtimeNow, releaseRealtime, retainRealtime, setRankMetric, setTopCount, startBoardAnalysis, startSingleAnalysis, clearSingle } from '@/stores/realtime'
import { refreshWatchNow, releaseWatch, retainWatch } from '@/stores/watch'
import { boardLabel, formatDateTime, formatGap, formatPct, formatPrice, formatScore, pctClass, spendWord } from '@/utils/format'
import { computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()

const top = computed(() => realtime.top)
const snapshot = computed(() => top.value?.snapshot ?? null)
const status = computed(() => realtime.status)
const report = computed(() => realtime.board.report)
const runnerStatus = computed(() => realtime.board.runner)
const boardBusy = computed(
  () => realtime.startingBoard || realtime.boardPending || runnerStatus.value.running === true,
)
const singleRunner = computed(() => realtime.single?.runner ?? null)
const singleBusy = computed(
  () => realtime.startingSingle || realtime.singlePending || singleRunner.value?.running === true,
)
const singleReport = computed(() => realtime.single?.report ?? null)
const singlePick = computed(() => singleReport.value?.picks?.[0] ?? null)

const boards = computed(() => {
  const snap = snapshot.value
  if (!snap) return []
  return [
    { key: 'limit-up', title: '已涨停', rows: snap.limit_up_rows, gap: false },
    { key: 'pending', title: '尚未涨停', rows: snap.pending_rows, gap: true },
  ]
})

const metrics = computed(() => top.value?.metrics ?? status.value?.metrics ?? [])
const selectedMetric = computed(() => top.value?.metric ?? realtime.metric)
const metricLabel = computed(() => top.value?.metric_label ?? '')

function countOptions(): number[] {
  const min = top.value?.top_n_min ?? 10
  const max = top.value?.top_n_max ?? 200
  return [min, 30, 50, 100, max].filter((value, index, all) => value <= max && all.indexOf(value) === index)
}

function gapOf(row: RealtimeRow): string {
  return formatGap(row.to_limit_percent)
}

/** 次日涨停率：`null` 是「这一档给不出」，画成「—」，不是 0%。 */
function rateOf(row: RealtimeRow): string {
  return row.next_day_rate_percent == null ? '—' : `${row.next_day_rate_percent.toFixed(2)}%`
}

function rateHint(row: RealtimeRow): string {
  if (!row.next_day_rate_samples) return ''
  const floor = snapshot.value?.probability?.min_band_observations ?? 50
  return row.next_day_rate_samples < floor
    ? `样本 ${row.next_day_rate_samples} · 不足 ${floor}`
    : `样本 ${row.next_day_rate_samples}`
}

function evidenceText(pick: AiPick): string {
  const evidence = pick.evidence
  const formula = evidence.formula
  const parts = [
    `相似 ${formatScore(evidence.max_similarity)}`,
    `平均 ${formatScore(evidence.avg_similarity)}`,
    `高相似 ${evidence.high_sim_count ?? '—'}/${evidence.sample_count ?? '—'}`,
    `距涨停 ${formatGap(evidence.to_limit_percent)}`,
    `综合 ${formula.has_signal ? formatScore(formula.composite_score) : '—'}`,
  ]
  return parts.join(' · ')
}

function historyText(code: string): string {
  const item = report.value?.pick_history?.[code]
  if (!item) return '没推荐过'
  if (item.settled === 0) return `推 ${item.picks} 次 · 待验证`
  return `推 ${item.picks} 次 · 定判 ${item.settled} 次 · 中 ${item.hits} · ${formatScore(item.hit_rate_percent)}%`
}

function analyze(row: RealtimeRow): void {
  void startSingleAnalysis(row.code)
}

function openSimilarity(row: RealtimeRow): void {
  const day = snapshot.value?.base_date
  if (!day) return
  void router.push({ name: 'similarity', query: { code: row.code, date: day } })
}

function onVisibility(): void {
  if (document.visibilityState !== 'visible') return
  refreshRealtimeNow(true)
  refreshWatchNow()
}

onMounted(() => {
  retainRealtime()
  retainWatch()
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  releaseRealtime()
  releaseWatch()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>盘中监控</h1>
      <p class="lede">
        {{ snapshot?.note || '榜还没取回来' }}
      </p>
    </div>
    <div class="toolbar">
      <span v-if="top" class="muted">榜前 {{ top.top_n }} 每 {{ top.refresh_seconds }} 秒 · 全市场每 {{ top.full_round_seconds }} 秒</span>
      <button class="btn btn-ghost" type="button" @click="refreshRealtimeNow(true)">刷新</button>
    </div>
  </header>

  <p v-if="realtime.error" class="alert">{{ realtime.error }}</p>
  <p v-else-if="realtime.note" class="hint">{{ realtime.note }}</p>

  <IntradayWatchPanel />

  <section class="card">
    <div class="card-head">
      <h2>选榜口径</h2>
      <div class="toolbar">
        <span class="muted">{{ metricLabel }}</span>
        <label class="field">
          <span>条数</span>
          <select :value="realtime.topN" @change="setTopCount(Number(($event.target as HTMLSelectElement).value))">
            <option v-for="value in countOptions()" :key="value" :value="value">{{ value }}</option>
          </select>
        </label>
      </div>
    </div>
    <div class="chips">
      <button
        v-for="item in metrics"
        :key="item.key"
        class="chip"
        :class="{ active: selectedMetric === item.key }"
        type="button"
        @click="setRankMetric(item.key)"
      >
        {{ item.label }}
      </button>
    </div>
    <div class="readonly" style="margin-top: 12px">
      <div class="two">
        <div>
          <div><span class="muted">时段</span> <b>{{ status?.phase || '—' }}</b><span v-if="status" class="muted"> · {{ status.in_session ? '盘中' : '非盘中' }}</span></div>
          <div><span class="muted">形态窗口</span> <b>{{ snapshot?.window_mode || '—' }}</b></div>
          <div><span class="muted">行情来源</span> <b>{{ snapshot?.provider || status?.source || '—' }}</b></div>
          <div><span class="muted">监控线程</span> <b>{{ status?.tracker_running ? '在跑' : '待命' }}</b></div>
        </div>
        <div>
          <div><span class="muted">上一轮全市场</span> <b>{{ formatDateTime(top?.full_round_at) }}</b><span v-if="top?.full_round_seconds_ago != null" class="muted"> · {{ top.full_round_seconds_ago }} 秒前</span></div>
          <div><span class="muted">上一次快刷</span> <b>{{ formatDateTime(top?.fast_round_at) }}</b><span v-if="top?.fast_round_seconds_ago != null" class="muted"> · {{ top.fast_round_seconds_ago }} 秒前</span></div>
          <div><span class="muted">全市场轮次 / 快刷轮次</span> <b>{{ top?.full_rounds ?? 0 }} / {{ top?.fast_rounds ?? 0 }}</b></div>
          <div><span class="muted">样本池</span> <b>{{ snapshot?.pool_size ?? 0 }} 条 · 截至 {{ snapshot?.base_date || '—' }}</b></div>
          <div><span class="muted">概率来源</span> <b>{{ snapshot ? (snapshot.probability?.run_id ? `第 ${snapshot.probability.run_id} 次回测` : '没有已完成回测') : '—' }}</b></div>
        </div>
      </div>
    </div>
  </section>

  <section class="stats">
    <article class="card stat">
      <div class="stat-label">已涨停</div>
      <div class="stat-value">{{ snapshot?.limit_up_rows.length ?? 0 }}</div>
      <div class="stat-foot">榜前 {{ top?.top_n ?? 0 }} 里</div>
    </article>
    <article class="card stat">
      <div class="stat-label">尚未涨停</div>
      <div class="stat-value">{{ snapshot?.pending_rows.length ?? 0 }}</div>
      <div class="stat-foot">榜前 {{ top?.top_n ?? 0 }} 里</div>
    </article>
    <article class="card stat">
      <div class="stat-label">全市场已打分</div>
      <div class="stat-value">{{ top?.market?.scanned ?? status?.full_rows ?? 0 }}</div>
      <div class="stat-foot">候选 {{ top?.leader_count ?? 0 }} 只</div>
    </article>
    <article class="card stat">
      <div class="stat-label">取不到相似度</div>
      <div class="stat-value">{{ top?.market?.skipped_count ?? snapshot?.skipped_count ?? 0 }}</div>
      <div class="stat-foot">{{ snapshot?.trade_date || '—' }} · {{ snapshot?.source || '—' }}</div>
    </article>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>AI 分析这一榜</h2>
      <div class="toolbar">
        <span v-if="!aiReady()" class="tag type-weak">AI 未配置</span>
        <button
          class="btn"
          type="button"
          :disabled="boardBusy || realtime.startingBoard || !snapshot || !aiReady()"
          @click="startBoardAnalysis"
        >
          {{ boardBusy ? '分析中…' : '分析这一榜' }}
        </button>
      </div>
    </div>
    <p v-if="realtime.actionError" class="alert">{{ realtime.actionError }}</p>

    <template v-if="boardBusy">
      <div class="progress running"><i :style="{ '--w': '100%' }" /></div>
      <div class="readonly" style="margin-top: 8px">
        <span class="muted">
          已跑 {{ runnerStatus.running_seconds ?? 0 }} 秒 · 正文 {{ runnerStatus.answer_chars ?? 0 }} 字 · 思考 {{ runnerStatus.thinking_chars ?? 0 }} 字
        </span>
      </div>
    </template>

    <template v-if="report">
      <p v-if="!report.ok" class="alert">
        {{ report.error }}
        <span class="muted"> · {{ spendWord(report) }}</span>
      </p>

      <div class="readonly">
        <div class="two">
          <div>
            <div><span class="muted">模型</span> <b>{{ report.model || '—' }}</b></div>
            <div><span class="muted">口径</span> <b>{{ report.metric_label || '—' }} · 榜前 {{ report.top_n ?? 0 }}</b></div>
            <div><span class="muted">交易日</span> <b>{{ report.trade_date || '—' }}</b><span class="muted"> · 截至 {{ report.base_date || '—' }}</span></div>
            <div><span class="muted">进提示词</span> <b>{{ report.feed_rows }}</b><span class="muted"> 行 / 榜 {{ report.board_rows }} 行</span></div>
          </div>
          <div>
            <div><span class="muted">耗时</span> <b>{{ report.latency_seconds }} 秒</b><span class="muted"> · {{ report.cost_text || '没有 token 用量' }}</span></div>
            <div><span class="muted">存档</span> <b>{{ report.report_id ? `#${report.report_id}` : '未存档' }}</b><span v-if="report.store_error" class="muted"> · {{ report.store_error }}</span></div>
            <div><span class="muted">校验闸</span> <b>池外 {{ report.dropped_codes.length }} · 重复 {{ report.duplicate_codes.length }} · 超限 {{ report.picks_beyond_limit }}</b></div>
            <div><span class="muted">response_format</span> <b>{{ report.response_format === undefined ? '—' : report.response_format ? '带' : '不带（网关不认，重发一次）' }}</b><span v-if="report.text_truncated" class="muted"> · 有文字被截</span></div>
          </div>
        </div>
      </div>

      <div v-if="report.criteria" class="card" style="margin-top: 12px">
        <div class="card-head"><h2>它的判断标准</h2><span v-if="report.criteria_truncated" class="tag type-weak">已截断</span></div>
        <p class="hint" style="white-space: pre-wrap">{{ report.criteria }}</p>
      </div>

      <div v-if="report.picks.length" class="table-wrap" style="margin-top: 12px; max-height: calc(100vh - 240px)">
        <table>
          <thead>
            <tr>
              <th>序</th>
              <th>股票</th>
              <th class="right">综合评分</th>
              <th class="right">AI 自报</th>
              <th class="right">差</th>
              <th>理由</th>
              <th>反证</th>
              <th>证据</th>
              <th>AI 老账</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(pick, index) in report.picks" :key="pick.stock_code">
              <td class="rank" :class="{ top: index < 3 }">{{ index + 1 }}</td>
              <td>
                <div>{{ pick.stock_name }}</div>
                <div class="code">{{ pick.stock_code }}</div>
              </td>
              <td class="right">
                <b>{{ pick.evidence.formula?.has_signal ? formatScore(pick.evidence.formula.composite_score) : '—' }}</b>
                <div class="hint">{{ pick.evidence.formula?.has_signal ? pick.evidence.formula.level : '' }}</div>
              </td>
              <td class="right">{{ pick.score == null ? '未给分' : formatScore(pick.score) }}</td>
              <td class="right">{{ pick.evidence.formula?.model_gap == null ? '—' : formatScore(pick.evidence.formula.model_gap) }}</td>
              <td style="max-width: 260px">{{ pick.reason || '—' }}</td>
              <td style="max-width: 260px">{{ pick.counter_evidence || '—' }}</td>
              <td class="hint">{{ evidenceText(pick) }}</td>
              <td class="hint">{{ historyText(pick.stock_code) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else-if="report.ok" class="empty">它没有给出池内推荐</div>

      <p class="hint" style="margin-top: 12px">{{ report.formula }}</p>
      <p class="hint">{{ report.disclaimer }}</p>
      <pre v-if="report.raw_excerpt" class="raw">{{ report.raw_excerpt }}</pre>
    </template>
    <div v-else-if="!boardBusy" class="empty">还没有这一份分析</div>
  </section>

  <section v-if="realtime.single || realtime.singleCode" class="card">
    <div class="card-head">
      <h2>单股直评 {{ realtime.singleCode }}</h2>
      <div class="toolbar">
        <button class="btn btn-ghost" type="button" @click="clearSingle">关掉</button>
      </div>
    </div>

    <template v-if="singleBusy">
      <div class="progress running"><i :style="{ '--w': '100%' }" /></div>
      <p class="hint">已跑 {{ singleRunner?.running_seconds ?? 0 }} 秒 · 正文 {{ singleRunner?.answer_chars ?? 0 }} 字</p>
    </template>

    <template v-if="singleReport">
      <p v-if="!singleReport.ok" class="alert">
        {{ singleReport.error }}
        <span class="muted"> · {{ spendWord(singleReport) }}</span>
      </p>
      <div v-if="singlePick" class="stack" style="margin-top: 8px">
        <div class="match-top">
          <div>
            <b>{{ singlePick.stock_name }}</b> <span class="code">{{ singlePick.stock_code }}</span>
            <span class="muted"> · {{ singlePick.evidence.board }} · {{ singlePick.evidence.at_limit ? '盘中已涨停' : '盘中未涨停' }}</span>
          </div>
          <div class="match-score">
            {{ singlePick.evidence.formula?.has_signal ? formatScore(singlePick.evidence.formula.composite_score) : '—' }}
            <span class="hint">{{ singlePick.evidence.formula?.has_signal ? singlePick.evidence.formula.level : '' }}</span>
          </div>
        </div>
        <div class="readonly">
          <div class="two">
            <div>
              <div><span class="muted">AI 自报</span> <b>{{ singlePick.score == null ? '未给分' : formatScore(singlePick.score) }}</b><span class="muted"> · 与公式差 {{ singlePick.evidence.formula?.model_gap == null ? '—' : formatScore(singlePick.evidence.formula.model_gap) }}</span></div>
              <div><span class="muted">现价</span> <b>{{ formatPrice(singlePick.evidence.last_price) }}</b><span :class="pctClass(singlePick.evidence.change_percent)"> {{ formatPct(singlePick.evidence.change_percent) }}</span><span class="muted"> · 距涨停 {{ formatGap(singlePick.evidence.to_limit_percent) }}</span></div>
              <div><span class="muted">涨停价</span> <b>{{ formatPrice(singlePick.evidence.limit_up_price) }}</b></div>
              <div><span class="muted">输入位次</span> <b>{{ singlePick.feed_rank ?? '—' }}</b><span class="muted"> · 档位 {{ singlePick.evidence.band || '—' }}</span></div>
            </div>
            <div>
              <div><span class="muted">证据</span> <b>{{ evidenceText(singlePick) }}</b></div>
              <div><span class="muted">模型</span> <b>{{ singleReport.model || '—' }}</b><span class="muted"> · {{ singleReport.latency_seconds }} 秒 · {{ singleReport.cost_text || '没有 token 用量' }}</span></div>
              <div><span class="muted">存档</span> <b>{{ singleReport.report_id ? `#${singleReport.report_id}` : '未存档' }}</b><span v-if="singleReport.store_error" class="muted"> · {{ singleReport.store_error }}</span></div>
              <div><span class="muted">老账</span> <b>{{ realtime.single?.ai_history ? `${realtime.single.ai_history.picks} 次推荐 · 定判 ${realtime.single.ai_history.settled} 次` : '没推荐过' }}</b></div>
            </div>
          </div>
        </div>
        <p><b>理由</b> {{ singlePick.reason || '—' }}</p>
        <p><b>反证</b> {{ singlePick.counter_evidence || '—' }}</p>
        <div v-if="singleReport.criteria" class="hint" style="white-space: pre-wrap">{{ singleReport.criteria }}</div>
      </div>
      <div v-else-if="singleReport.ok" class="empty">这一只没有给出结论</div>
      <p class="hint" style="margin-top: 8px">{{ singleReport.disclaimer }}</p>
    </template>
    <div v-else-if="!singleBusy" class="empty">点一行的「直评」问这一只</div>
  </section>

  <section v-for="board in boards" :key="board.key" class="card">
    <div class="card-head">
      <h2>{{ board.title }}</h2>
      <div class="toolbar">
        <span class="muted">{{ snapshot?.limit_up_count ?? 0 }} 已涨停 / {{ snapshot?.pending_count ?? 0 }} 未涨停 · 全市场 {{ top?.market?.scanned ?? 0 }} 只 · 跳过 {{ top?.market?.skipped_count ?? 0 }} 只</span>
      </div>
    </div>
    <div v-if="!board.rows.length" class="empty">这一榜这轮没有行</div>
    <div v-else class="table-wrap" style="max-height: calc(100vh - 260px)">
      <table>
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>市场</th>
            <th class="right">现价</th>
            <th class="right">涨幅</th>
            <th v-if="board.gap" class="right">距涨停</th>
            <th class="right">涨停价</th>
            <th class="right">综合相似</th>
            <th class="right">形态</th>
            <th class="right">量</th>
            <th class="right">量比</th>
            <th class="right">平均相似</th>
            <th class="right">高相似/样本</th>
            <th>档位</th>
            <th class="right">次日涨停率</th>
            <th>最相似样本</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in board.rows" :key="row.code">
            <td class="code">{{ row.code }}</td>
            <td>{{ row.name }}</td>
            <td><span class="pill" :class="row.exchange === 'SH' ? 'pill-sh' : 'pill-sz'">{{ boardLabel(row.exchange) }}</span></td>
            <td class="right">{{ formatPrice(row.latest) }}</td>
            <td class="right" :class="pctClass(row.pct_chg)">{{ formatPct(row.pct_chg) }}</td>
            <td v-if="board.gap" class="right">{{ gapOf(row) }}</td>
            <td class="right">{{ formatPrice(row.limit_up_price) }}</td>
            <td class="right">
              <span class="meter"><i :style="{ '--w': `${Math.min(100, row.score ?? 0)}%` }" /></span>{{ formatScore(row.score) }}
            </td>
            <td class="right">{{ formatScore(row.shape_score) }}</td>
            <td class="right">{{ formatScore(row.volume_score) }}</td>
            <td class="right">{{ formatScore(row.volume_pct_score) }}</td>
            <td class="right">{{ formatScore(row.avg_score) }}</td>
            <td class="right">{{ row.high_score_count }}/{{ row.sample_count }}</td>
            <td><span class="tag">{{ row.band }}</span></td>
            <td class="right">
              <b>{{ rateOf(row) }}</b>
              <div class="hint">{{ rateHint(row) }}</div>
            </td>
            <td>
              <span v-if="row.best_match">{{ row.best_match.name }} {{ row.best_match.limit_date }}</span>
              <span v-else>—</span>
              <div class="hint" v-if="row.best_match">{{ boardLabel(row.best_match.code.startsWith('6') ? 'SH' : 'SZ') }} {{ row.best_match.code }} · {{ formatScore(row.best_match.score) }}</div>
            </td>
            <td>
              <div class="toolbar">
                <button class="btn btn-ghost" type="button" :disabled="!aiReady() || singleBusy" @click="analyze(row)">直评</button>
                <button class="btn btn-ghost" type="button" @click="openSimilarity(row)">对比</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section v-if="snapshot" class="card">
    <div class="card-head"><h2>这一榜的口径</h2></div>
    <p class="hint">{{ snapshot.probability?.note }}</p>
    <p class="hint">{{ snapshot.disclaimer }}</p>
  </section>
</template>
