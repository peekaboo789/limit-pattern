<script setup lang="ts">
import { watch as watchStore, ackWatch } from '@/stores/watch'
import type { WatchAlert, WatchRow } from '@/api/types'
import { formatClock, formatPct, formatPrice, formatScore, pctClass } from '@/utils/format'
import { computed } from 'vue'

/**
 * 盘中「涨幅报警榜」：前一交易日 AI 固定榜的那几名，今天单拎出来盯涨幅。
 *
 * 这一层只读**后端已经采好的那一份**（`GET /api/realtime/intraday-watch`），不在请求里敲行情源：
 * 采样是后台线程按 `poll_seconds` 的节奏在跑的，所以「页面没开」和「窗口收进托盘」都不影响报警。
 * 阈值（3%／6%／近 3 分钟跳升）与档位标识也全从后端回包里读——页面这里没有开关可以拧，
 * 一把尺子配两个读数处迟早对不上，读回来的数字必须就是判据用的那个。
 *
 * 三处纪律跟着整个系统一贯的口径：
 *
 * 1. **空就是空，不许变成数字**。`rank`/`score` 是前一天那份模型输出，读不出显示「—」而不是 0 名；
 *    `rise_3min` 在采样不满 3 分钟时是 `null`（「还不知道它有没有在冲」），不是 0.0（「它没在冲」）。
 * 2. **五种状态各说各话**：盘中监控 / 还没开盘 / 已经收盘 / 没有池子 / 这轮没拿到行情，
 *    后端各写了一句人话（`note`），页面原样摆出来，不折叠成一句「暂无数据」。
 * 3. **点「知道了」当场生效**。放声与标题闪烁那本账在 store 里（`stores/watch.ts`），
 *    这里只负责把数字摆出来。
 */

const state = computed(() => watchStore.state)
const rows = computed<WatchRow[]>(() => state.value?.rows ?? [])
const unacked = computed<WatchAlert[]>(() => state.value?.unacked ?? [])
const pendingSpikes = computed(() =>
  unacked.value.filter((item) => item.tier === state.value?.pin_tier),
)

/** 一句话说明这一档此刻在干什么；五种状态不折叠成一句「暂无数据」 */
const statusWord = computed(() => {
  const body = state.value
  if (!body) return '正在连接涨幅报警榜…'
  if (body.status === 'watching') return `盘中监控中：${body.row_count} 只，每 ${body.poll_seconds} 秒采一轮`
  const text: Record<string, string> = {
    pre_open: '还没开盘：这一段不敲行情源，开盘后自动开始盯',
    after_close: '已经收盘：价格不再变，没点「知道了」的置顶还会继续摞着窗口',
    no_pool: '今天没有可盯的池子',
    unavailable: '这一轮没拿到今天的行情',
  }
  return text[body.status] ?? `状态 ${body.status}`
})

/**
 * 采样那一句只用后端给的数字拼，前端不留一份常量。
 * 「盯哪一天的哪几名」与「涨过多少报一次」由后端 `note` 与 `disclaimer` 自己说，这里不重抄一遍。
 */
const samplingSentence = computed(() => {
  const s = state.value?.sampling
  if (!s) return ''
  const parts = [
    `后台已采 ${s.rounds} 轮`,
    s.last_round_seconds_ago === null ? '还没跑过一轮' : `最近一轮 ${s.last_round_seconds_ago} 秒前`,
    s.source ? `数据源 ${s.source}` : '数据源未报',
  ]
  if (!s.thread_running) parts.push('监控线程当前没在跑（有人读这一档时它会自己起来）')
  if (s.error) parts.push(`这一轮的错：${s.error}`)
  return parts.join(' · ')
})

function pointsText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(2)} 点`
}

function rankText(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : String(value)
}


function isPin(item: { tier: string }): boolean {
  return item.tier === state.value?.pin_tier
}

/**
 * 一行的状态标签。四种「报过」的写法必须分得开**报过没确认**与**报过已确认**：
 * 后者是用户已经看过的那一条，页面再把它标成待确认，等于让用户重复点同一件事。
 * 档位那句话用库里的 `tier_label`（后端给的），前端不另写一份「涨过 6%」。
 */
function rowState(row: WatchRow): string {
  const pin = row.spike
  if (pin) return pin.acked_at ? `${pin.tier_label}（已确认）` : `${pin.tier_label}·待确认`
  const alert = row.alert
  if (alert) return alert.acked_at ? `${alert.tier_label}（已确认）` : `${alert.tier_label}·待确认`
  return '盯盘中'
}

function reachedThreshold(row: WatchRow): boolean {
  const body = state.value
  if (!body || row.change_percent === null) return false
  return row.change_percent >= body.alert_percent
}

/** 只有「报过且没确认」的那一行才给按钮：已确认的行再放一颗「知道了」是颗按了没反应的按钮 */
function pendingOf(row: WatchRow): boolean {
  return (
    (row.alert !== null && row.alert !== undefined && !row.alert.acked_at) ||
    (row.spike !== null && row.spike !== undefined && !row.spike.acked_at)
  )
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="watch-title">
        <h2>涨幅报警榜</h2>
        <span class="muted">{{ statusWord }}</span>
        <span v-if="samplingSentence" class="muted">{{ samplingSentence }}</span>
      </div>
      <button v-if="unacked.length" class="btn" type="button" :disabled="watchStore.ackBusy" @click="ackWatch()">
        全部知道了（{{ unacked.length }} 条）
      </button>
    </div>

    <p v-if="watchStore.error" class="alert">{{ watchStore.error }}</p>
    <p v-else-if="watchStore.note" class="hint">{{ watchStore.note }}</p>

    <!-- 榜在场时这句挂在表上面；榜不在场时同一句话改由下面的 `.empty` 说一次，两处都渲染就成了重复 -->
    <p v-if="rows.length && state?.note" class="hint">{{ state.note }}</p>

    <div v-if="rows.length" class="table-wrap">
      <table class="watch-table">
        <thead>
          <tr>
            <th class="right">名次</th>
            <th>股票</th>
            <th class="right">昨日分</th>
            <th class="right">最新价</th>
            <th class="right">今日涨幅</th>
            <th class="right">
              近 {{ Math.round((state?.rise_window_seconds ?? 180) / 60) }} 分钟跳升 ≥ {{ state?.rise_min_points ?? '—' }} 点
            </th>
            <th>状态</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.stock_code">
            <td class="right rank" :class="{ top: row.rank !== null && row.rank <= 3 }">{{ rankText(row.rank) }}</td>
            <td>
              <div>{{ row.stock_name }}</div>
              <div class="code">{{ row.stock_code }}</div>
            </td>
            <td class="right">{{ formatScore(row.score) }}</td>
            <td class="right">{{ formatPrice(row.last_price) }}</td>
            <td class="right" :class="reachedThreshold(row) ? 'up' : pctClass(row.change_percent)">{{ formatPct(row.change_percent) }}</td>
            <td class="right">
              {{ pointsText(row.rise_3min) }}
              <div v-if="row.rise_3min === null && !row.no_snapshot" class="hint">
                采样 {{ row.sample_span_seconds ?? 0 }} 秒
              </div>
            </td>
            <td><span class="tag" :class="row.spike && !row.spike.acked_at ? 'type-weak' : 'tag-hot'">{{ rowState(row) }}</span></td>
            <td>
              <button
                v-if="pendingOf(row)"
                class="btn btn-ghost"
                type="button"
                :disabled="watchStore.ackBusy"
                @click="ackWatch(row.stock_code)"
              >
                知道了
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty">
      {{ state?.note || '这一档没有可盯的股票：池子还没凑齐（还没跑过第一轮采样），不是今天没有一只在涨' }}
    </div>

    <p v-if="state" class="hint">{{ state.disclaimer }}</p>

    <!--
      报警浮层：只要库里还有「没确认」的报警就一直摆着，吸在视口上，不跟着滚走。
      声音那句必须说实话：浏览器要先有了一次点击才允许网页出声，没解锁时明说，不假装响过。
    -->
    <Teleport to="body">
      <div v-if="unacked.length" class="watch-alert">
        <div class="watch-alert__head">
          <span>涨幅报警：{{ unacked.length }} 条待确认</span>
          <span v-if="pendingSpikes.length" class="watch-alert__pin">
            其中 {{ pendingSpikes.length }} 只已把窗口摞到最前面，点「知道了」才解除
          </span>
        </div>
        <ul class="watch-alert__list">
          <li v-for="item in unacked" :key="`${item.trade_date}-${item.stock_code}-${item.tier}`">
            <span class="tag" :class="isPin(item) ? 'type-weak' : 'tag-hot'">{{ item.tier_label }}</span>
            <span class="code">{{ item.stock_code }}</span>
            <span>{{ item.stock_name }}</span>
            <span class="up">{{ formatPct(item.percent) }}</span>
            <span class="hint">
              {{ item.rank === null ? '名次未知' : `第 ${item.rank} 名` }} ·
              {{ item.rise_3min === null ? '跳升未知' : `跳升 ${item.rise_3min.toFixed(2)} 点` }} ·
              {{ formatClock(item.fired_at) }}
            </span>
            <button class="btn btn-ghost" type="button" :disabled="watchStore.ackBusy" @click="ackWatch(item.stock_code, item.tier)">
              知道了
            </button>
          </li>
        </ul>
        <div class="watch-alert__foot">
          <span v-if="!watchStore.soundReady" class="hint">
            声音还没解锁：浏览器要先点一下页面才允许网页出声，点过之后新的报警才会响。
          </span>
          <span v-else class="hint">报警声已经可以响。</span>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.watch-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  min-width: 0;
}

/* 这张表的行不能点：全局给 `tbody tr` 配的指针光标在这里是个假承诺 */
.watch-table tbody tr {
  cursor: default;
}

/* 报警浮层：底色用不透明的深色而不是玻璃——这一层要的是「看见」，不是融进页面。 */
.watch-alert {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 3000;
  width: min(440px, calc(100vw - 32px));
  padding: 12px 14px;
  border: 1px solid var(--line, rgba(160, 186, 224, 0.14));
  border-left: 4px solid var(--up);
  border-radius: 12px;
  background: #121a2b;
  color: var(--text);
  box-shadow: 0 12px 30px rgba(0, 0, 0, 0.55);
}
.watch-alert__head {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-weight: 600;
  margin-bottom: 8px;
}
.watch-alert__pin {
  color: var(--up);
  font-size: 12px;
  font-weight: 500;
}
.watch-alert__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 40vh;
  overflow-y: auto;
}
.watch-alert__list li {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 13px;
}
.watch-alert__list .btn {
  height: 26px;
  padding: 0 10px;
  font-size: 12px;
}
.watch-alert__foot {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 8px;
}
</style>
