<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { PatternDetailResponse, PatternOverview, PatternSample } from '@/api/types'
import Disclaimer from '@/components/Disclaimer.vue'
import { loadHealth, session } from '@/stores/session'
import { formatPct, formatScore, pctClass, shortDate } from '@/utils/format'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const dateInput = ref('')
const loading = ref(false)
const detailLoading = ref(false)
const error = ref('')
const overview = ref<PatternOverview | null>(null)
const detail = ref<PatternDetailResponse | null>(null)
const selectedDay = ref('')
const selected = ref<PatternSample | null>(null)

const days = computed(() => overview.value?.days ?? [])
const activeDay = computed(() => days.value.find((item) => item.limit_date === selectedDay.value) ?? days.value[0] ?? null)

/** 库里选中样本那条的明细：明细接口按代码返回全部涨停日，这里只挑页面上点的那一天。 */
const activeSample = computed(() => {
  if (!detail.value || !selected.value) return null
  return detail.value.samples.find((item) => item.limit_date === selected.value?.limit_date) ?? null
})

async function ensureDate() {
  if (!session.health) await loadHealth()
  if (!dateInput.value) dateInput.value = session.health?.data_date ?? ''
}

async function openSample(sample: PatternSample) {
  selected.value = sample
  detailLoading.value = true
  error.value = ''
  try {
    detail.value = await api.patternDetail(sample.code)
  } catch (reason) {
    detail.value = null
    error.value = reason instanceof ApiError ? reason.detail : '样本明细加载失败'
  } finally {
    detailLoading.value = false
  }
}

async function load(keepSelection = false) {
  await ensureDate()
  if (!dateInput.value) {
    error.value = '库里还没有行情'
    return
  }
  loading.value = true
  error.value = ''
  try {
    overview.value = await api.patternsOverview(dateInput.value)
    const first = overview.value.days.find((item) => item.count > 0) ?? overview.value.days[0]
    if (!keepSelection || !overview.value.days.some((item) => item.limit_date === selectedDay.value)) {
      selectedDay.value = first?.limit_date ?? ''
    }
    if (!keepSelection) {
      selected.value = null
      detail.value = null
    }
  } catch (reason) {
    overview.value = null
    error.value = reason instanceof ApiError ? reason.detail : '样本库读数失败'
  } finally {
    loading.value = false
  }
  const next = { date: dateInput.value }
  if (route.query.date !== dateInput.value) await router.replace({ name: 'patterns', query: next })
}

function pickDay(day: string) {
  selectedDay.value = day
  // 换到别的一天就把明细收起来：留着上一天那条，面板指的就是眼前这张表里没有的行。
  if (selected.value && selected.value.limit_date !== day) {
    selected.value = null
    detail.value = null
  }
}

onMounted(() => void load())

watch(
  () => route.query.date,
  (value) => {
    const date = typeof value === 'string' ? value : ''
    if (date && date !== dateInput.value) {
      dateInput.value = date
      void load()
    }
  },
)
</script>

<template>
  <header class="page-head">
    <div>
      <h1>涨停形态样本</h1>
      <p class="lede">{{ overview?.trade_date ?? '—' }} 往前 {{ overview?.pool_dates.length ?? 0 }} 个交易日的已收盘涨停样本库。</p>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>基准日</span>
        <input v-model="dateInput" class="mono" type="text" maxlength="10" placeholder="YYYY-MM-DD" @keyup.enter="load()" />
      </label>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load()">{{ loading ? '读库中…' : '刷新' }}</button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <div v-if="loading && !overview" class="skeleton" />

  <section v-if="overview" class="card">
    <div class="card-head">
      <h2>样本池</h2>
      <span class="muted">库里共 {{ overview.sample_total }} 条 · 向量 {{ overview.dimension }} 维</span>
    </div>
    <Disclaimer :text="overview.disclaimer" />
    <div class="chips" style="margin-top: 4px">
      <button
        v-for="day in days"
        :key="day.limit_date"
        class="chip"
        :class="{ active: activeDay?.limit_date === day.limit_date }"
        type="button"
        @click="pickDay(day.limit_date)"
      >
        {{ shortDate(day.limit_date) }} · {{ day.count }}
      </button>
    </div>
    <p v-if="!overview.pool_is_current" class="alert" style="margin-top: 10px">
      样本池这几天库里一行样本都没有：先在数据管理页点「重建涨停样本」。
    </p>
  </section>

  <div v-if="activeDay" class="split">
    <section class="card">
      <div class="card-head">
        <h2>{{ activeDay.limit_date }} 涨停样本</h2>
        <span class="muted">{{ activeDay.count }} 条</span>
      </div>
      <p v-if="Object.keys(activeDay.skipped).length" class="muted" style="margin-top: 0">
        闸门挡掉：
        <span v-for="(count, reason) in activeDay.skipped" :key="reason" style="margin-right: 10px">
          {{ overview?.reason_labels[reason] ?? reason }} {{ count }}
        </span>
      </p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th class="right">涨跌幅</th>
              <th>形态三天</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in activeDay.items"
              :key="item.code + item.limit_date"
              :class="{ active: selected?.code === item.code && selected?.limit_date === item.limit_date }"
              @click="openSample(item)"
            >
              <td class="code">{{ item.code }}</td>
              <td>{{ item.name }}</td>
              <td class="right" :class="item.change_percent && item.change_percent > 0 ? 'up' : 'down'">
                {{ formatPct(item.change_percent) }}
              </td>
              <td class="mono muted">{{ item.window.map(shortDate).join(' / ') }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!activeDay.items.length" class="empty">这一天库里没有样本</p>
      </div>
    </section>

    <div class="stack">
      <section v-if="selected" class="card">
        <div class="card-head">
          <h2>{{ selected.name }} · 涨停前形态</h2>
          <span class="muted code">{{ selected.code }} · 涨停 {{ selected.limit_date }}</span>
        </div>
        <div v-if="detailLoading" class="skeleton" />
        <template v-else-if="activeSample">
          <Disclaimer :text="detail?.disclaimer ?? ''" />
          <div class="table-wrap" style="max-height: none">
            <table>
              <thead>
                <tr>
                  <th>日期</th>
                  <th class="right">开盘</th>
                  <th class="right">最高</th>
                  <th class="right">最低</th>
                  <th class="right">收盘</th>
                  <th class="right">涨跌幅</th>
                  <th class="right">量比5日</th>
                  <th class="right">换手率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="day in activeSample.days ?? []" :key="day.trade_date">
                  <td class="mono">{{ shortDate(day.trade_date) }}</td>
                  <td class="right">{{ day.quote.open ?? '—' }}</td>
                  <td class="right">{{ day.quote.high ?? '—' }}</td>
                  <td class="right">{{ day.quote.low ?? '—' }}</td>
                  <td class="right">{{ day.quote.close ?? '—' }}</td>
                  <td class="right" :class="pctClass(day.quote.change_percent)">{{ formatPct(day.quote.change_percent) }}</td>
                  <td class="right">{{ formatScore(day.features.volume_ratio_5 ?? null) }}</td>
                  <td class="right">{{ day.quote.turnover_rate ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="table-wrap" style="max-height: none">
            <table>
              <thead>
                <tr>
                  <th>三日聚合</th>
                  <th class="right">数值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(value, name) in activeSample.aggregates ?? {}" :key="String(name)">
                  <td class="muted">{{ name }}</td>
                  <td class="right mono">{{ value.toFixed(4) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="hint">
            涨停日 {{ selected.limit_date }} 本身不进这条样本的向量，形态只有它前面这 {{ (activeSample.days ?? []).length }} 根。
          </p>
        </template>
        <p v-else class="empty">库里没有这一条的明细，重新重建涨停样本后再看</p>
      </section>

      <section v-else class="card">
        <div class="card-head"><h2>样本明细</h2></div>
        <p class="empty">左边点一条样本</p>
      </section>
    </div>
  </div>
</template>
