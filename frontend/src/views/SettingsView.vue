<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { Settings, SourceTestResult } from '@/api/types'
import { formatDateTime } from '@/utils/format'
import { sourceLabel } from '@/utils/labels'
import { computed, onMounted, reactive, ref } from 'vue'

const FALLBACK_SOURCES = ['auto', 'eastmoney', 'tencent', 'sina']

const meta = ref<Settings | null>(null)
const form = reactive({
  retention_trading_days: 60,
  compare_window_trading_days: 3,
  sample_pool_trading_days: 30,
  ai_base_url: '',
  ai_model: '',
  data_source: 'auto',
  ai_api_key: '',
  ai_thinking: 'auto',
  ai_timeout_seconds: 120,
  ai_batch_size: 10,
  ai_parallel: 3,
  euclidean_similarity_weight: 0.8,
  similarity_threshold_high: 90,
  alert_trading_hours_only: true,
  alert_include_auction: false,
  watchlist_alerts: true,
})
const sourceOptions = computed(() =>
  meta.value?.data_source_options?.length ? meta.value.data_source_options : FALLBACK_SOURCES,
)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const saved = ref(false)

function apply(settings: Settings) {
  meta.value = settings
  form.retention_trading_days = settings.retention_trading_days
  form.compare_window_trading_days = settings.compare_window_trading_days
  form.sample_pool_trading_days = settings.sample_pool_trading_days ?? 30
  form.ai_base_url = settings.ai_base_url
  form.ai_model = settings.ai_model
  form.data_source = settings.data_source || 'auto'
  form.ai_api_key = ''
  form.ai_thinking = settings.ai_thinking || 'auto'
  form.ai_timeout_seconds = settings.ai_timeout_seconds ?? 120
  form.ai_batch_size = settings.ai_batch_size ?? 10
  form.ai_parallel = settings.ai_parallel ?? 3
  form.euclidean_similarity_weight = settings.euclidean_similarity_weight ?? 0.8
  form.similarity_threshold_high = settings.similarity_threshold_high ?? 90
  form.alert_trading_hours_only = settings.alert_trading_hours_only !== false
  form.alert_include_auction = settings.alert_include_auction === true
  form.watchlist_alerts = settings.watchlist_alerts !== false
}

const switching = ref(false)
const switchHint = ref('')
const testing = ref(false)
const testResults = ref<SourceTestResult[]>([])

const aiTesting = ref(false)
const aiResult = ref<{ ok: boolean; text: string } | null>(null)

async function testAi() {
  if (aiTesting.value) return
  aiTesting.value = true
  aiResult.value = null
  try {
    const result = await api.testAi({
      ai_base_url: form.ai_base_url.trim(),
      ai_model: form.ai_model.trim(),
      ai_api_key: form.ai_api_key.trim() || undefined,
      ai_thinking: form.ai_thinking,
    })
    const note = result.note ? `（${result.note}）` : ''
    aiResult.value = result.ok
      ? { ok: true, text: `连接成功，用时 ${result.latency_ms} 毫秒，深度思考：${result.thinking ?? ''}，模型回复：${result.reply}。调用地址 ${result.endpoint}${note}` }
      : { ok: false, text: `${result.error}${note}` }
  } catch (reason) {
    aiResult.value = { ok: false, text: reason instanceof ApiError ? reason.detail : '测试请求没有返回' }
  } finally {
    aiTesting.value = false
  }
}

async function chooseSource(item: string) {
  if (switching.value) return
  const previous = form.data_source
  form.data_source = item
  switching.value = true
  switchHint.value = ''
  try {
    const next = await api.saveSettings({ data_source: item })
    meta.value = next
    switchHint.value = `已切换到「${sourceLabel(item)}」并保存，正在进行的同步会对后面的股票立即改用它。`
  } catch (reason) {
    form.data_source = previous
    switchHint.value = reason instanceof ApiError ? reason.detail : '切换失败'
  } finally {
    switching.value = false
  }
}

async function testSources() {
  testing.value = true
  try {
    testResults.value = (await api.testSources(8)).results
  } catch (reason) {
    testResults.value = []
    switchHint.value = reason instanceof ApiError ? reason.detail : '测试失败'
  } finally {
    testing.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    apply(await api.settings())
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '设置加载失败'
  } finally {
    loading.value = false
  }
}

function validate(): string {
  const pool = Number(form.sample_pool_trading_days)
  const keep = Number(form.retention_trading_days)
  if (!Number.isInteger(pool) || pool < 3 || pool > 60) return '涨停样本池要是 3 到 60 的整数'
  const floor = Math.max(pool, 30) + 8
  if (!Number.isInteger(keep) || keep < floor || keep > 250) return `保留天数要是 ${floor} 到 250 的整数（至少 max(样本池,30)+8）`
  const eu = Number(form.euclidean_similarity_weight)
  if (!Number.isFinite(eu) || eu < 0 || eu > 1) return '欧氏分权重要在 0 到 1 之间'
  const high = Number(form.similarity_threshold_high)
  if (!Number.isFinite(high) || high < 60 || high > 100) return '高度相似阈值要在 60 到 100 之间'
  const timeout = Number(form.ai_timeout_seconds)
  if (!Number.isInteger(timeout) || timeout < 30 || timeout > 600) return 'AI 超时要是 30 到 600 的整数'
  const batch = Number(form.ai_batch_size)
  const parallel = Number(form.ai_parallel)
  if (!Number.isInteger(batch) || batch < 1 || batch > 40) return '每批股票数要是 1 到 40 的整数'
  if (!Number.isInteger(parallel) || parallel < 1 || parallel > 8) return '并发批数要是 1 到 8 的整数'
  return ''
}

async function save() {
  const problem = validate()
  if (problem) {
    error.value = problem
    return
  }
  saved.value = false
  error.value = ''
  if (!sourceOptions.value.includes(form.data_source)) {
    error.value = '数据源只能是 auto、eastmoney、tencent、sina'
    return
  }
  saving.value = true
  try {
    const body: {
      retention_trading_days: number
      sample_pool_trading_days: number
      ai_base_url: string
      ai_model: string
      data_source: string
      ai_api_key?: string
      ai_thinking: string
      ai_timeout_seconds: number
      ai_batch_size: number
      ai_parallel: number
      euclidean_similarity_weight: number
      cosine_similarity_weight: number
      similarity_threshold_high: number
      alert_trading_hours_only: boolean
      alert_include_auction: boolean
      watchlist_alerts: boolean
    } = {
      retention_trading_days: form.retention_trading_days,
      sample_pool_trading_days: form.sample_pool_trading_days,
      ai_base_url: form.ai_base_url.trim(),
      ai_model: form.ai_model.trim(),
      data_source: form.data_source,
      ai_thinking: form.ai_thinking,
      ai_timeout_seconds: Number(form.ai_timeout_seconds),
      ai_batch_size: Number(form.ai_batch_size),
      ai_parallel: Number(form.ai_parallel),
      euclidean_similarity_weight: Number(form.euclidean_similarity_weight),
      cosine_similarity_weight: Math.round((1 - Number(form.euclidean_similarity_weight)) * 1000) / 1000,
      similarity_threshold_high: Number(form.similarity_threshold_high),
      alert_trading_hours_only: form.alert_trading_hours_only,
      alert_include_auction: form.alert_include_auction,
      watchlist_alerts: form.watchlist_alerts,
    }
    if (form.ai_api_key.trim()) body.ai_api_key = form.ai_api_key.trim()
    apply(await api.saveSettings(body))
    saved.value = true
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.detail : '保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1>设置</h1>
      <p class="lede">涨停明细保留多久、对比窗口、行情源，以及每日总结用的 AI 接口。</p>
    </div>
    <button class="btn" type="button" :disabled="saving || loading" @click="save">{{ saving ? '保存中…' : '保存设置' }}</button>
  </header>

  <p v-if="error" class="alert">{{ error }}</p>
  <p v-else-if="saved" class="alert alert-ok">已保存。密钥不会回显。</p>
  <div v-if="loading" class="skeleton" />

  <form v-else class="settings-grid" @submit.prevent="save">
    <section class="card">
      <div class="card-head"><h2>数据</h2></div>
      <label class="field">
        <span>涨停明细保留交易日</span>
        <input v-model.number="form.retention_trading_days" type="number" :min="Math.max(form.sample_pool_trading_days || 30, 30) + 8" max="250" step="1" />
        <span class="hint">默认 60，最多 250，至少 max(样本池, 30) + 8（3 根特征 K 线 + 5 日量比基准）。形态计算另外保留 max(样本池,30)+63 天 K 线（position_60 用），不受这里影响。当前生效：{{ meta?.retention_trading_days ?? '-' }} 个交易日。到期明细会清理，每日总结 JSON 永久保留。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>涨停样本池（近 N 个交易日）</span>
        <input v-model.number="form.sample_pool_trading_days" type="number" min="3" max="60" step="1" />
        <span class="hint">默认 30，可改 3 到 60。相似度、AI 排名、聚类、次日池和回测只和这段时间里涨停过的股票比。形态特征固定看涨停前 3 根 K 线。当前生效：{{ meta?.sample_pool_trading_days ?? '-' }} 个交易日。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>相似度：欧氏分权重（余弦分 = 1 − 该值）</span>
        <input v-model.number="form.euclidean_similarity_weight" type="number" min="0" max="1" step="0.05" />
        <span class="hint">默认 0.8 / 0.2。相似度 = 欧氏分 × 该值 + 余弦分 × (1 − 该值)；欧氏分 = 100·exp(−(d/尺度)²)，尺度 = 样本池两两距离中位数。当前：{{ meta?.euclidean_similarity_weight ?? 0.8 }} / {{ meta?.cosine_similarity_weight ?? 0.2 }}</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>高度相似阈值</span>
        <input v-model.number="form.similarity_threshold_high" type="number" min="60" max="100" step="1" />
        <span class="hint">默认 90。相似度 ≥ 该值的样本计入「高度相似数」，也决定历史成功率的取样。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span><input v-model="form.alert_trading_hours_only" type="checkbox" /> 仅在交易时段报警</span>
        <span class="hint">默认开：只在交易日 9:30–11:30、13:00–15:00 弹 ≥3% 提醒和 ≥6% 红框；盘前、午休、收盘后、周末节假日只显示最后行情，也不拉行情。「测试提醒」任何时候都能用。</span>
      </label>
      <label class="field" style="margin-top: 8px">
        <span><input v-model="form.alert_include_auction" type="checkbox" :disabled="!form.alert_trading_hours_only" /> 集合竞价 9:15–9:25 也报警</span>
        <span class="hint">默认关。竞价价格会撤单变动，容易误报。</span>
      </label>
      <label class="field" style="margin-top: 8px">
        <span><input v-model="form.watchlist_alerts" type="checkbox" /> 自选股报警</span>
        <span class="hint">默认开：自选股涨幅 ≥3% 弹提醒、≥6% 红框，阈值和交易时段规则与 AI 榜单监控相同。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>数据源</span>
        <div class="chips" role="radiogroup" aria-label="数据源">
          <button
            v-for="item in sourceOptions"
            :key="item"
            class="chip"
            :class="{ active: form.data_source === item }"
            type="button"
            role="radio"
            :aria-checked="form.data_source === item"
            :disabled="switching"
            @click="chooseSource(item)"
          >
            {{ sourceLabel(item) }}
          </button>
        </div>
        <span class="hint">当前选择「{{ sourceLabel(form.data_source) }}」。实际连上的源是「{{ sourceLabel(meta?.active_source || '') || '还没有' }}」。点选即生效并保存。</span>
        <span v-if="switchHint" class="hint">{{ switchHint }}</span>
      </label>
      <div class="toolbar" style="margin-top: 8px">
        <button class="btn btn-ghost" type="button" :disabled="testing" @click="testSources">{{ testing ? '测试中（最多 8 秒）…' : '测试数据源' }}</button>
      </div>
      <table v-if="testResults.length" style="margin-top: 8px">
        <thead><tr><th>数据源</th><th>状态</th><th class="right">耗时</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="row in testResults" :key="row.source">
            <td>{{ sourceLabel(row.source) }}</td>
            <td :class="row.ok ? 'down' : 'up'">{{ row.ok ? '可用' : '不可用' }}</td>
            <td class="right">{{ row.latency_ms }} ms</td>
            <td class="wrap muted">{{ row.ok ? `最新 ${row.latest}` : row.error }}</td>
          </tr>
        </tbody>
      </table>
      <div class="readonly">
        <span class="muted">纳入范围</span>
        <b>{{ meta?.market_scope || '上证主板、深证主板、创业板' }}</b>
      </div>
      <p class="hint">默认不含科创板（688/689）。查询科创板代码会提示「科创板不在纳入范围」。要用环境变量 EXCLUDED_BOARDS 调整。</p>
      <div class="readonly">
        <span class="muted">上次同步</span>
        <b>{{ formatDateTime(meta?.last_sync_at) }}</b>
      </div>
      <div class="readonly">
        <span class="muted">上次清理</span>
        <b>{{ formatDateTime(meta?.last_cleanup_at) }}</b>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>AI 总结</h2>
        <span class="tag" :class="meta?.ai_api_key_set ? 'tag-hot' : 'type-weak'">
          {{ meta?.ai_api_key_set ? '密钥已设置' : '未设置密钥' }}
        </span>
      </div>
      <div class="readonly">
        <span class="muted">模型解读</span>
        <b>{{ meta?.ai_enabled ? '已启用' : '未启用' }}</b>
      </div>
      <p class="hint">地址、模型和密钥都填上之后才会启用。没配齐时重新生成仍会写出统计总结，不会报错。</p>
      <label class="field" style="margin-top: 12px">
        <span>接口地址</span>
        <input v-model="form.ai_base_url" placeholder="https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" />
        <span class="hint">填到 /v1 为止，程序会自动加 /chat/completions。阿里云百炼填「…aliyuncs.com/compatible-mode/v1」，只填域名会自动补上。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>模型</span>
        <input v-model="form.ai_model" placeholder="deepseek-chat" />
      </label>
      <label class="field" style="margin-top: 12px">
        <span>深度思考</span>
        <select v-model="form.ai_thinking">
          <option value="auto">自动（不发送参数，报错时自动改用可用的值并记住）</option>
          <option value="on">开</option>
          <option value="off">关</option>
        </select>
        <span class="hint">有的模型只能开思考（如 qwen3.8 系列），开启思考或使用阿里云时一律流式返回：只要模型还在输出（包括思考过程）就不算超时。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>AI 超时（秒）</span>
        <input v-model.number="form.ai_timeout_seconds" type="number" min="30" max="600" />
        <span class="hint">30–600，默认 120：连续这么多秒收不到任何数据才算超时；每批总时长最多 max(600, 该值) 秒。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>AI 排名每批股票数 / 并发批数</span>
        <span style="display: flex; gap: 8px">
          <input v-model.number="form.ai_batch_size" type="number" min="1" max="40" style="width: 6em" />
          <input v-model.number="form.ai_parallel" type="number" min="1" max="8" style="width: 6em" />
        </span>
        <span class="hint">默认每批 10 只、同时 3 批。慢模型调小每批数量；某批超时会自动拆成两半各重试一次。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>API Key</span>
        <input v-model="form.ai_api_key" type="password" autocomplete="new-password" placeholder="留空则不修改" />
        <span class="hint">只写入，接口不会把密钥返回来。留空表示保持原密钥。</span>
      </label>
      <div style="margin-top: 12px">
        <button class="btn" type="button" :disabled="aiTesting" @click="testAi">{{ aiTesting ? '测试中…' : '测试 AI 接口' }}</button>
        <span class="hint" style="margin-left: 8px">用表单里的值（未保存也可以）发一句很短的话。</span>
      </div>
      <p v-if="aiResult" class="hint" :style="{ color: aiResult.ok ? '#16a34a' : '#dc2626', wordBreak: 'break-all' }">{{ aiResult.text }}</p>
    </section>
  </form>
</template>
