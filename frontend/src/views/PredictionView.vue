<script setup lang="ts">
import { api, ApiError, isAbort } from '@/api/client'
import type { PredictionPool, PredictionRow } from '@/api/types'
import { formatPct, formatPrice, formatScore, pctClass } from '@/utils/format'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const pool = ref<PredictionPool | null>(null)
const day = ref('')
const loading = ref(false)
const error = ref('')

/** 发生率是 0~1 的原始比例；空表示「这一档算不出来」，不是 0%。 */
function rateText(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(2)}%`
}

/** 连板率在服务端就是百分数，别再乘一次。 */
function percentText(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toFixed(2)}%`
}

function liftText(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toFixed(2)}×`
}

function termsText(row: PredictionRow): string {
  const terms = row.score_terms
  const carry = terms.carry == null ? '—' : formatScore(terms.carry)
  const band = terms.band == null ? '—' : formatScore(terms.band)
  return `形态 ${formatScore(terms.shape)} · 连板 ${carry} · 档位 ${band}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    pool.value = await api.predictionPool({ date: day.value || undefined })
  } catch (reason) {
    if (isAbort(reason)) return
    error.value = reason instanceof ApiError ? reason.detail : '预测池加载失败'
  } finally {
    loading.value = false
  }
}

function open(row: PredictionRow) {
  if (!pool.value) return
  void router.push({ name: 'similarity', query: { code: row.stock_code, date: pool.value.pool_date } })
}

onMounted(() => {
  void load()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>次日预测池</h1>
      <p class="lede">当日未涨停的股票里，按形态相似度、相似样本的连板情况与档位历史发生率加权排序。</p>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>截至</span>
        <input v-model="day" type="date" @change="load" />
      </label>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load">
        {{ loading ? '取数中…' : '刷新' }}
      </button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <div v-else-if="loading && !pool" class="skeleton" />
  <p v-else-if="pool" class="hint">{{ pool.note }}</p>

  <template v-if="pool">
    <section class="stats">
      <article class="card stat">
        <div class="stat-label">挑选日</div>
        <div class="stat-value" style="font-size: 20px">{{ pool.pool_date }}</div>
        <div class="stat-foot">{{ pool.pool_dates.join(' / ') }}</div>
      </article>
      <article class="card stat">
        <div class="stat-label">目标交易日</div>
        <div class="stat-value" style="font-size: 20px">{{ pool.next_trade_date || '—' }}</div>
        <div class="stat-foot">日历里挑选日之后的第一天</div>
      </article>
      <article class="card stat">
        <div class="stat-label">入池 / 参与打分</div>
        <div class="stat-value">{{ pool.rows.length }} / {{ pool.pool_total }}</div>
        <div class="stat-foot">涨停样本池 {{ pool.sample_pool_size }} 条</div>
      </article>
      <article class="card stat">
        <div class="stat-label">概率来源</div>
        <div class="stat-value" style="font-size: 20px">{{ pool.backtest_run_id ? `第 ${pool.backtest_run_id} 次回测` : '没有回测' }}</div>
        <div class="stat-foot">{{ pool.calibrated ? `${pool.backtest_day_count} 天 · ${pool.backtest_observation_count} 条观测` : '档位项与档内次日留空' }}</div>
      </article>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>档位底表</h2>
        <span class="muted">基准 {{ rateText(pool.baseline_rate) }} · 每档至少 {{ pool.min_band_observations }} 条观测才算数</span>
      </div>
      <div v-if="!pool.bands.length" class="empty">还没有已完成回测的分档统计，档位项与档内次日留空</div>
      <div v-else class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>档位</th>
              <th class="right">观测</th>
              <th class="right">次日涨停</th>
              <th class="right">发生率</th>
              <th class="right">相对基准</th>
              <th class="right">待揭晓</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="band in pool.bands" :key="band.band">
              <td><span class="tag">{{ band.band }}</span></td>
              <td class="right">{{ band.sample_count }}</td>
              <td class="right">{{ band.limit_up_count }}</td>
              <td class="right"><b>{{ rateText(band.limit_up_rate) }}</b></td>
              <td class="right">{{ liftText(band.lift) }}</td>
              <td class="right">{{ band.pending_count }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>预测池</h2>
        <div class="toolbar">
          <span class="muted">前 {{ pool.pool_size }} 名 · 实际 {{ pool.rows.length }} 只</span>
          <span v-if="pool.missing_bars" class="chip">缺 K 线 {{ pool.missing_bars }}</span>
          <span v-if="pool.dirty_skipped" class="chip">脏行剔 {{ pool.dirty_skipped }}</span>
          <span v-if="pool.skipped_samples.length" class="chip">跳过样本 {{ pool.skipped_samples.length }}</span>
        </div>
      </div>
      <div v-if="!pool.rows.length" class="empty">这一天没有可入池的股票</div>
      <div v-else class="table-wrap" style="max-height: calc(100vh - 300px)">
        <table>
          <thead>
            <tr>
              <th>名次</th>
              <th>股票</th>
              <th>板块</th>
              <th class="right">收盘</th>
              <th class="right">涨幅</th>
              <th class="right">最高相似</th>
              <th class="right">平均</th>
              <th class="right">高相似/样本</th>
              <th>档位</th>
              <th class="right">档内次日</th>
              <th class="right">连板延续</th>
              <th class="right">预测加权分</th>
              <th>三项</th>
              <th class="right">综合评分</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in pool.rows" :key="row.stock_code">
              <td class="rank" :class="{ top: row.rank <= 3 }">{{ row.rank }}</td>
              <td>
                <div>{{ row.stock_name }}<span v-if="row.is_st" class="tag type-weak" style="margin-left: 6px">ST</span></div>
                <div class="code">{{ row.stock_code }}</div>
              </td>
              <td>{{ row.board_label }}</td>
              <td class="right">{{ formatPrice(row.close) }}</td>
              <td class="right" :class="pctClass(row.change_percent)">{{ formatPct(row.change_percent) }}</td>
              <td class="right">{{ formatScore(row.max_similarity) }}</td>
              <td class="right">{{ formatScore(row.avg_similarity) }}</td>
              <td class="right">{{ row.high_similarity_count }}/{{ row.sample_count }}</td>
              <td><span class="tag">{{ row.band }}</span></td>
              <td class="right">
                <b>{{ rateText(row.band_rate) }}</b>
                <div class="hint">
                  {{ row.band_samples ? `样本 ${row.band_samples}${row.band_samples < pool.min_band_observations ? ` · 不足 ${pool.min_band_observations}` : ''}` : '这一档没数' }}
                </div>
              </td>
              <td class="right">
                <b>{{ percentText(row.carry_rate) }}</b>
                <div class="hint">{{ row.carry_hit_count }}/{{ row.carry_known_count }} · 缺 {{ row.carry_missing_count }}</div>
              </td>
              <td class="right"><b>{{ formatScore(row.prediction_score) }}</b></td>
              <td class="hint">{{ termsText(row) }}</td>
              <td class="right">
                <b>{{ row.formula.has_signal ? formatScore(row.formula.composite_score) : '—' }}</b>
                <div class="hint">{{ row.formula.has_signal ? row.formula.level : '' }}</div>
              </td>
              <td>
                <button class="btn btn-ghost" type="button" @click="open(row)">对比</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="hint" style="margin-top: 12px">{{ pool.formula }}</p>
      <p class="hint">{{ pool.disclaimer }}</p>
    </section>
  </template>
</template>
