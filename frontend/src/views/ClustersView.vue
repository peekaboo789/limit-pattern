<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { ClusterAssignmentOut, ClusterOverviewOut } from '@/api/types'
import ClusterScatter from '@/components/ClusterScatter.vue'
import Disclaimer from '@/components/Disclaimer.vue'
import { loadHealth, session } from '@/stores/session'
import { formatPct, formatTurnover, shortDate } from '@/utils/format'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const dateInput = ref('')
const kChoice = ref<number | 0>(0)
const loading = ref(false)
const assignLoading = ref(false)
const error = ref('')
const assignError = ref('')
const overview = ref<ClusterOverviewOut | null>(null)
const assignment = ref<ClusterAssignmentOut | null>(null)
const selectedId = ref<number | null>(null)
const targetCode = ref('')

const kOptions = computed(() => {
  const top = overview.value?.k_max ?? 6
  const bottom = overview.value?.k_min ?? 2
  const list: { value: number; label: string }[] = [{ value: 0, label: '自动（轮廓系数）' }]
  for (let k = bottom; k <= top; k += 1) list.push({ value: k, label: `${k} 组` })
  return list
})

const groups = computed(() => overview.value?.clusters ?? [])
const activeGroup = computed(
  () => groups.value.find((item) => item.cluster_id === selectedId.value) ?? groups.value[0] ?? null,
)
const biggest = computed(() =>
  groups.value.reduce((max, item) => Math.max(max, item.share), 0),
)
/** 归属表要跟分组表讲同一个故事：两边吃同一个 k，页面上不留两套组。 */
const kParam = computed(() => (kChoice.value === 0 ? undefined : kChoice.value))

async function ensureDate() {
  if (!session.health) await loadHealth()
  if (!dateInput.value) dateInput.value = session.health?.data_date ?? ''
}

async function load() {
  await ensureDate()
  if (!dateInput.value) {
    error.value = '库里还没有行情'
    return
  }
  loading.value = true
  error.value = ''
  try {
    overview.value = await api.clustersOverview({ date: dateInput.value, k: kParam.value })
    if (!groups.value.some((item) => item.cluster_id === selectedId.value)) selectedId.value = null
  } catch (reason) {
    overview.value = null
    selectedId.value = null
    error.value = reason instanceof ApiError ? reason.detail : '形态聚类读数失败'
  } finally {
    loading.value = false
  }
  if (targetCode.value.trim()) await lookup()
  const next = { date: dateInput.value }
  if (route.query.date !== dateInput.value) await router.replace({ name: 'clusters', query: next })
}

async function lookup() {
  const code = targetCode.value.trim()
  if (!code) {
    assignError.value = '先填股票代码'
    assignment.value = null
    return
  }
  assignLoading.value = true
  assignError.value = ''
  try {
    assignment.value = await api.clusterAssignment(code, {
      date: dateInput.value || undefined,
      k: kParam.value,
    })
  } catch (reason) {
    assignment.value = null
    assignError.value = reason instanceof ApiError ? reason.detail : '形态归属读数失败'
  } finally {
    assignLoading.value = false
  }
}

function openMember(code: string) {
  void router.push({ name: 'similarity', query: { code, date: dateInput.value } })
}

function signed(value: number | null): string {
  return value == null ? '—' : formatPct(value, 1)
}

onMounted(() => void load())

watch(kChoice, () => void load())
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
      <h1>形态聚类</h1>
      <p class="lede">
        {{ overview?.trade_date ?? '—' }} 往前 {{ overview?.pool_dates.length ?? 0 }} 个交易日的
        {{ overview?.sample_count ?? 0 }} 条涨停样本，分成 {{ overview?.k ?? 0 }} 组。
      </p>
    </div>
    <div class="toolbar">
      <label class="field">
        <span>基准日</span>
        <input v-model="dateInput" class="mono" type="text" maxlength="10" placeholder="YYYY-MM-DD" @keyup.enter="load()" />
      </label>
      <label class="field">
        <span>分组数</span>
        <select v-model.number="kChoice" class="mono">
          <option v-for="item in kOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <button class="btn btn-ghost" type="button" :disabled="loading" @click="load()">
        {{ loading ? '算组中…' : '刷新' }}
      </button>
    </div>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <div v-if="loading && !overview" class="skeleton" />

  <template v-if="overview">
    <section class="stats">
      <div class="card stat">
        <div class="stat-label">涨停样本</div>
        <div class="stat-value">{{ overview.sample_count }}</div>
        <div class="stat-foot">
          {{ overview.pool_dates.map(shortDate).join(' / ')
          }}{{ overview.ignored_stale ? ` · 旧口径跳过 ${overview.ignored_stale} 条` : '' }}
        </div>
      </div>
      <div class="card stat">
        <div class="stat-label">形态组数</div>
        <div class="stat-value">{{ overview.k }}</div>
        <div class="stat-foot">{{ overview.selected_by }}</div>
      </div>
      <div class="card stat">
        <div class="stat-label">轮廓系数</div>
        <div class="stat-value">{{ overview.silhouette.toFixed(4) }}</div>
        <div class="stat-foot">
          {{ overview.evaluated.length ? `候选 ${overview.evaluated.map((item) => item.k).join('/')}` : '页面指定' }}
        </div>
      </div>
      <div class="card stat">
        <div class="stat-label">最大组占比</div>
        <div class="stat-value">{{ (biggest * 100).toFixed(1) }}%</div>
        <div class="stat-foot">{{ groups.length }} 组 · 高相似线 {{ overview.high_band }} 分</div>
      </div>
    </section>

    <Disclaimer :text="overview.disclaimer" />

    <div class="split">
      <section class="card">
        <div class="card-head">
          <h2>形态组</h2>
          <span class="muted">{{ groups.length }} 组</span>
        </div>
        <div class="chips" style="flex-direction: column; align-items: stretch">
          <button
            v-for="group in groups"
            :key="group.cluster_id"
            class="chip"
            :class="{ active: activeGroup?.cluster_id === group.cluster_id }"
            type="button"
            @click="selectedId = group.cluster_id"
          >
            {{ group.name }} · {{ group.size }} 条 · {{ (group.share * 100).toFixed(1) }}%
          </button>
        </div>
      </section>

      <div class="stack">
        <section v-if="activeGroup" class="card">
          <div class="card-head">
            <h2>{{ activeGroup.name }}</h2>
            <span class="muted">代表股 {{ activeGroup.representative.code }} {{ activeGroup.representative.name }}</span>
          </div>
          <div class="table-wrap" style="max-height: none">
            <table>
              <thead>
                <tr>
                  <th>组内三日形态中位数</th>
                  <th class="right">数值</th>
                </tr>
              </thead>
              <tbody>
                <tr><td class="muted">累计涨幅</td><td class="right mono">{{ signed(activeGroup.signature.return_3d) }}</td></tr>
                <tr><td class="muted">收盘趋势</td><td class="right mono">{{ signed(activeGroup.signature.slope_close) }}</td></tr>
                <tr><td class="muted">三日波动</td><td class="right mono">{{ activeGroup.signature.volatility_3d.toFixed(2) }}%</td></tr>
                <tr><td class="muted">量能倍数</td><td class="right mono">{{ activeGroup.signature.volume_trend.toFixed(2) }}×</td></tr>
                <tr><td class="muted">三日振幅</td><td class="right mono">{{ activeGroup.signature.amplitude_sum.toFixed(1) }}%</td></tr>
                <tr><td class="muted">日均振幅</td><td class="right mono">{{ activeGroup.signature.amplitude_mean.toFixed(1) }}%</td></tr>
                <tr><td class="muted">T-1 换手率</td><td class="right mono">{{ formatTurnover(activeGroup.signature.turnover_rate) }}</td></tr>
                <tr><td class="muted">T-1 20 日位置</td><td class="right mono">{{ (activeGroup.signature.position_20 * 100).toFixed(0) }}%</td></tr>
                <tr><td class="muted">组内平均距离</td><td class="right mono">{{ activeGroup.intra_distance.toFixed(4) }}</td></tr>
                <tr><td class="muted">与最近邻组距离</td><td class="right mono">{{ activeGroup.nearest_other_distance.toFixed(4) }}</td></tr>
                <tr><td class="muted">组间区分度</td><td class="right mono">{{ activeGroup.separation.toFixed(4) }}</td></tr>
              </tbody>
            </table>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>涨停日</th>
                  <th>形态三天</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in activeGroup.members"
                  :key="item.code + item.limit_date"
                  @click="openMember(item.code)"
                >
                  <td class="code">{{ item.code }}</td>
                  <td>
                    {{ item.name }}
                    <span v-if="item.is_representative" class="tag tag-hot">代表</span>
                  </td>
                  <td class="mono">{{ item.limit_date }}</td>
                  <td class="mono muted">{{ item.window.map(shortDate).join(' / ') }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="!activeGroup.members.length" class="empty">这一组没有成员</p>
          </div>
        </section>
      </div>
    </div>

    <section class="card">
      <div class="card-head">
        <h2>形态散点</h2>
        <span class="muted">{{ overview.points.length }} 个点</span>
      </div>
      <ClusterScatter v-if="overview.points.length" :points="overview.points" :groups="overview.clusters" />
      <p v-else class="empty">样本不足以做二维投影</p>
    </section>

    <section v-if="overview.evaluated.length" class="card">
      <div class="card-head">
        <h2>分组数评估</h2>
        <span class="muted">采用 {{ overview.k }} 组</span>
      </div>
      <div class="table-wrap" style="max-height: none">
        <table>
          <thead>
            <tr>
              <th>分组数</th>
              <th class="right">轮廓系数</th>
              <th>是否采用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in overview.evaluated" :key="item.k" :class="{ active: item.k === overview.k }">
              <td class="mono">{{ item.k }}</td>
              <td class="right mono">{{ item.silhouette.toFixed(4) }}</td>
              <td class="muted">{{ item.k === overview.k ? '采用' : '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>目标股归属</h2>
        <span class="muted">可比样本 {{ assignment?.sample_count ?? overview.sample_count }} 条 · 分组数 {{ assignment?.k ?? overview.k }}</span>
      </div>
      <div class="toolbar">
        <label class="field">
          <span>股票代码</span>
          <input v-model="targetCode" class="mono" type="text" maxlength="6" placeholder="6 位代码" @keyup.enter="lookup()" />
        </label>
        <button class="btn" type="button" :disabled="assignLoading" @click="lookup()">
          {{ assignLoading ? '比对中…' : '查归属' }}
        </button>
      </div>
      <p v-if="assignError" class="alert" style="margin-top: 10px">{{ assignError }}</p>
      <template v-if="assignment">
        <p v-if="assignment.skipped_reason" class="alert" style="margin-top: 10px">{{ assignment.skipped_reason }}</p>
        <p v-else class="lede" style="margin-top: 10px">
          {{ assignment.code }} {{ assignment.name }} 最像
          <strong>{{ assignment.assigned_cluster_name }}</strong>
          ，组内均分领先次优组 {{ assignment.margin.toFixed(2) }} 分 · 形态三天
          <span class="mono">{{ assignment.window.map(shortDate).join(' / ') }}</span>
        </p>
        <div v-if="assignment.groups.length" class="table-wrap" style="margin-top: 10px">
          <table>
            <thead>
              <tr>
                <th>形态组</th>
                <th class="right">可比样本</th>
                <th class="right">组内均分</th>
                <th class="right">组内最高分</th>
                <th class="right">过 {{ assignment.high_band }} 分</th>
                <th>组内最像的样本</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in assignment.groups"
                :key="item.cluster_id"
                :class="{ active: item.cluster_id === assignment.assigned_cluster_id }"
              >
                <td>{{ item.name }}</td>
                <td class="right mono">{{ item.size }}</td>
                <td class="right mono">{{ item.avg_similarity.toFixed(2) }}</td>
                <td class="right mono">{{ item.max_similarity.toFixed(2) }}</td>
                <td class="right mono">{{ item.high_band_count }}</td>
                <td class="muted mono">
                  {{ item.top_members.map((member) => `${member.code} ${shortDate(member.limit_date)} ${member.similarity?.toFixed(2) ?? '—'}`).join(' · ') }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>
  </template>

  <section v-else-if="!loading" class="card">
    <div class="card-head"><h2>形态组</h2></div>
    <p class="empty">库里这一天没有可聚类的涨停样本</p>
  </section>
</template>
