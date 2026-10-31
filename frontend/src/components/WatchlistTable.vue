<script setup lang="ts">
import type { WatchRow } from '@/api/types'
import { watch as wl } from '@/stores/watchlist'
import { computed } from 'vue'

/** 自选股实时行情表；compact 用在总览 / 实时监控页。 */
const props = withDefaults(defineProps<{ compact?: boolean; limit?: number }>(), { compact: false, limit: 0 })
const active = computed(() => wl.monitor?.alerts_active === true)
const rows = computed<WatchRow[]>(() => {
  const list = wl.monitor?.items ?? []
  return props.limit ? list.slice(0, props.limit) : list
})
const hasAlert = computed(() => active.value && rows.value.some((r) => r.level === 'alert'))
function n(v: number | null | undefined, d = 2) {
  return v == null ? '—' : v.toFixed(d)
}
function pct(v: number | null) {
  return v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}
</script>

<template>
  <section class="card monitor-panel watch-table" :class="{ 'panel-alert': hasAlert }">
    <div class="card-head">
      <h2>自选股 · 实时监控</h2>
      <span class="toolbar">
        <span class="market-chip" :class="{ on: active && wl.monitor?.market_open }">{{ wl.monitor?.status_text ?? '…' }}</span>
        <RouterLink v-if="props.compact" class="btn btn-ghost" to="/watchlist">管理自选股</RouterLink>
      </span>
    </div>
    <p v-if="!wl.items.length" class="muted">还没有自选股，到「自选股」页添加。</p>
    <template v-else>
      <p class="muted small" style="margin: 0 0 6px">
        {{ wl.items.length }} 只<template v-if="active"> · 超 3% {{ wl.monitor?.warn_count ?? 0 }} 只，超 6% {{ wl.monitor?.alert_count ?? 0 }} 只</template>
        <template v-else-if="wl.monitor?.price_label"> · {{ wl.monitor.price_label }}</template>
        <template v-if="wl.monitor?.source"> · 行情 {{ wl.monitor.source }}</template>
        <template v-if="wl.monitor?.updated_at"> · 更新 {{ wl.monitor.updated_at.slice(11, 19) }}</template>
      </p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>股票</th><th class="right">现价</th><th class="right">涨跌幅</th><th class="right">量比</th><th class="right">最高</th><th class="right">最低</th>
              <th v-if="!props.compact" class="right">相似度</th><th v-if="!props.compact" class="right">综合分</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.code" :class="active ? 'lv-' + r.level : ''">
              <td>{{ r.name || r.code }} <span class="code muted">{{ r.code }}</span>
                <span v-if="r.is_limit_up" class="tag up">涨停</span>
                <span v-if="r.star" class="tag" title="科创板不在相似度样本池">科创板</span></td>
              <td class="right">{{ n(r.price) }}</td>
              <td class="right" :class="r.pct == null ? '' : r.pct > 0 ? 'up' : r.pct < 0 ? 'down' : ''">{{ pct(r.pct) }}</td>
              <td class="right">{{ n(r.volume_ratio) }}</td>
              <td class="right">{{ n(r.high) }}</td>
              <td class="right">{{ n(r.low) }}</td>
              <td v-if="!props.compact" class="right" :title="r.score_from ?? ''">{{ n(r.max_similarity, 1) }}</td>
              <td v-if="!props.compact" class="right" :title="r.score_from ?? ''">{{ n(r.composite_score, 1) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.up { color: #ef4444; }
.down { color: #22c55e; }
.tag { font-size: 11px; padding: 1px 6px; border-radius: 4px; background: rgba(160, 186, 224, 0.12); margin-left: 4px; }
.tag.up { background: rgba(239, 68, 68, 0.15); }
tr.lv-warn td:first-child { box-shadow: inset 3px 0 0 #f59e0b; }
tr.lv-alert td:first-child { box-shadow: inset 3px 0 0 #ef4444; }
</style>
