<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { Settings } from '@/api/types'
import { formatDateTime } from '@/utils/format'
import { sourceLabel } from '@/utils/labels'
import { computed, onMounted, reactive, ref } from 'vue'

const FALLBACK_SOURCES = ['auto', 'eastmoney', 'tencent', 'sina']

const meta = ref<Settings | null>(null)
const form = reactive({
  retention_trading_days: 5,
  compare_window_trading_days: 3,
  ai_base_url: '',
  ai_model: '',
  data_source: 'auto',
  ai_api_key: '',
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
  form.ai_base_url = settings.ai_base_url
  form.ai_model = settings.ai_model
  form.data_source = settings.data_source || 'auto'
  form.ai_api_key = ''
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

async function save() {
  saved.value = false
  error.value = ''
  if (!Number.isInteger(form.retention_trading_days) || form.retention_trading_days < 3 || form.retention_trading_days > 30) {
    error.value = '保留天数要在 3 到 30 之间'
    return
  }
  if (!sourceOptions.value.includes(form.data_source)) {
    error.value = '数据源只能是 auto、eastmoney、tencent、sina'
    return
  }
  saving.value = true
  try {
    const body: {
      retention_trading_days: number
      ai_base_url: string
      ai_model: string
      data_source: string
      ai_api_key?: string
    } = {
      retention_trading_days: form.retention_trading_days,
      ai_base_url: form.ai_base_url.trim(),
      ai_model: form.ai_model.trim(),
      data_source: form.data_source,
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
        <input v-model.number="form.retention_trading_days" type="number" min="3" max="30" step="1" />
        <span class="hint">默认 5，可改 3 到 30。到期明细会清理，每日总结 JSON 会留下。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>对比窗口（交易日）</span>
        <input :value="form.compare_window_trading_days" type="number" readonly />
        <span class="hint">固定为 3 个交易日，不能修改。相似度只和最近涨停样本比。</span>
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
            @click="form.data_source = item"
          >
            {{ sourceLabel(item) }}
          </button>
        </div>
        <span class="hint">当前选择「{{ sourceLabel(form.data_source) }}」。实际连上的源是「{{ sourceLabel(meta?.active_source || '') || '还没有' }}」。</span>
      </label>
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
        <input v-model="form.ai_base_url" placeholder="https://api.example.com/v1" />
      </label>
      <label class="field" style="margin-top: 12px">
        <span>模型</span>
        <input v-model="form.ai_model" placeholder="deepseek-chat" />
      </label>
      <label class="field" style="margin-top: 12px">
        <span>API Key</span>
        <input v-model="form.ai_api_key" type="password" autocomplete="new-password" placeholder="留空则不修改" />
        <span class="hint">只写入，接口不会把密钥返回来。留空表示保持原密钥。</span>
      </label>
    </section>
  </form>
</template>
