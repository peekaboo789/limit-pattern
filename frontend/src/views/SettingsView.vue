<script setup lang="ts">
import { api, ApiError } from '@/api/client'
import type { AiSourceRow, AiSourcesResponse, Settings } from '@/api/types'
import { loadSettings, session } from '@/stores/session'
import { formatDateTime } from '@/utils/format'
import { sourceLabel } from '@/utils/labels'
import { computed, onMounted, reactive, ref } from 'vue'

const FALLBACK_SOURCES = ['auto', 'eastmoney', 'tencent', 'sina']

const form = reactive({
  retention_trading_days: 5,
  compare_window_trading_days: 3,
  ai_base_url: '',
  ai_model: '',
  data_source: 'auto',
  ai_api_key: '',
})
const meta = computed(() => session.settings)
const sourceOptions = computed(() =>
  meta.value?.data_source_options?.length ? meta.value.data_source_options : FALLBACK_SOURCES,
)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const saved = ref(false)

function apply(settings: Settings) {
  session.settings = settings
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

// --- 大模型网关清单：加一条、改一条、删一条、切当前 --------------------------------
//
// 「切当前」之后必须把 `session.settings` 一起换掉：三条花钱链路的按钮判的是
// `ai_enabled`，它跟着「当前那一条配没配齐」走。这里交值，别的页面就不该重进一次才更新。

const gateways = ref<AiSourcesResponse | null>(null)
const gatewayError = ref('')
const gatewayBusy = ref(false)
const editingId = ref('')
const gatewayDraft = reactive({ name: '', base_url: '', model: '', api_key: '' })

const gatewayRows = computed<AiSourceRow[]>(() => gateways.value?.sources ?? [])
const activeRow = computed<AiSourceRow | null>(
  () => gatewayRows.value.find((item) => item.active) ?? null,
)

async function loadGateways() {
  try {
    gateways.value = await api.aiSources()
    gatewayError.value = ''
  } catch (reason) {
    gatewayError.value = reason instanceof ApiError ? reason.detail : '网关清单加载失败'
  }
}

function clearGatewayDraft() {
  editingId.value = ''
  gatewayDraft.name = ''
  gatewayDraft.base_url = ''
  gatewayDraft.model = ''
  gatewayDraft.api_key = ''
}

/** 编辑时不把打码后的地址回填：那一份带着 `***`，存回去每次都是 401。 */
function startGatewayEdit(row: AiSourceRow) {
  editingId.value = row.id
  gatewayDraft.name = row.name
  gatewayDraft.base_url = ''
  gatewayDraft.model = row.model
  gatewayDraft.api_key = ''
}

async function submitGateway(action: () => Promise<AiSourcesResponse>, fallback: string) {
  gatewayBusy.value = true
  gatewayError.value = ''
  try {
    gateways.value = await action()
    await loadSettings()
    clearGatewayDraft()
  } catch (reason) {
    gatewayError.value = reason instanceof ApiError ? reason.detail : fallback
  } finally {
    gatewayBusy.value = false
  }
}

function addGateway() {
  void submitGateway(() => api.createAiSource({ ...gatewayDraft }), '新增网关失败')
}

function saveGatewayEdit() {
  void submitGateway(() => api.updateAiSource(editingId.value, { ...gatewayDraft }), '修改网关失败')
}

function removeGateway(row: AiSourceRow) {
  if (!window.confirm(`删掉「${row.name}」？清单里存的那把密钥会一起没掉。`)) return
  void submitGateway(() => api.deleteAiSource(row.id), '删除网关失败')
}

function activateGateway(row: AiSourceRow) {
  void submitGateway(() => api.activateAiSource(row.id), '切换网关失败')
}

async function save() {
  saved.value = false
  error.value = ''
  if (!Number.isInteger(form.retention_trading_days) || form.retention_trading_days < 3 || form.retention_trading_days > 30) {
    error.value = '保留天数要在 3 到 30 之间'
    return
  }
  const windowMin = meta.value?.compare_window_min ?? 1
  if (
    !Number.isInteger(form.compare_window_trading_days) ||
    form.compare_window_trading_days < windowMin ||
    form.compare_window_trading_days > form.retention_trading_days
  ) {
    error.value = `对比窗口要在 ${windowMin} 到保留天数 ${form.retention_trading_days} 之间`
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
      compare_window_trading_days: number
      ai_base_url: string
      ai_model: string
      data_source: string
      ai_api_key?: string
    } = {
      retention_trading_days: form.retention_trading_days,
      compare_window_trading_days: form.compare_window_trading_days,
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
  void loadGateways()
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
        <input
          v-model.number="form.compare_window_trading_days"
          type="number"
          :min="meta?.compare_window_min ?? 1"
          :max="form.retention_trading_days"
          step="1"
        />
        <span class="hint">默认 3，可改 {{ meta?.compare_window_min ?? 1 }} 到保留天数。</span>
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
        <span class="tag" :class="meta?.ai_active_key_present ? 'tag-hot' : 'type-weak'">
          {{ meta?.ai_active_key_present ? '密钥已配' : '密钥没配' }}
        </span>
      </div>
      <div class="readonly">
        <span class="muted">模型解读</span>
        <b>{{ meta?.ai_enabled ? '已启用' : '未启用' }}</b>
      </div>
      <div class="readonly">
        <span class="muted">生效网关</span>
        <b>{{ meta?.ai_active_name || '—' }}</b>
      </div>
      <div class="readonly">
        <span class="muted">生效地址</span>
        <b>{{ meta?.ai_active_base_url || '—' }}</b>
      </div>
      <div class="readonly">
        <span class="muted">生效模型</span>
        <b>{{ meta?.ai_active_model || '—' }}</b>
      </div>
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

  <form class="settings-grid" @submit.prevent="editingId ? saveGatewayEdit() : addGateway()">
    <section class="card">
      <div class="card-head">
        <h2>大模型网关</h2>
        <div class="toolbar">
          <span class="muted">{{ gateways?.custom_count ?? 0 }} / {{ gateways?.max_sources ?? 0 }} 条 · 当前「{{ gateways?.active_name || '—' }}」</span>
        </div>
      </div>
      <p v-if="gatewayError" class="alert">{{ gatewayError }}</p>
      <div v-if="!gateways" class="skeleton" />
      <template v-else>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>模型</th>
                <th>地址</th>
                <th>密钥</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in gatewayRows" :key="row.id">
                <td>
                  <div>{{ row.name }}</div>
                  <div v-if="editingId === row.id" class="hint">编辑中</div>
                </td>
                <td class="code">{{ row.model || '—' }}</td>
                <td class="code">{{ row.base_url || '—' }}</td>
                <td><span class="tag" :class="row.key_present ? 'tag-hot' : 'type-weak'">{{ row.key_present ? '已存' : '没有' }}</span></td>
                <td>
                  <span class="tag" :class="row.complete ? 'tag-hot' : 'type-weak'">{{ row.complete ? '三项齐' : '没配齐' }}</span>
                </td>
                <td>
                  <div class="toolbar">
                    <button class="btn btn-ghost" type="button" :disabled="gatewayBusy || row.active" @click="activateGateway(row)">
                      {{ row.active ? '当前' : '设为当前' }}
                    </button>
                    <button v-if="!row.builtin" class="btn btn-ghost" type="button" :disabled="gatewayBusy" @click="startGatewayEdit(row)">编辑</button>
                    <button v-if="!row.builtin" class="btn btn-ghost" type="button" :disabled="gatewayBusy || row.active" @click="removeGateway(row)">删除</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="gateways.note" class="alert" style="margin-top: 10px">{{ gateways.note }}</p>
        <div class="readonly" style="margin-top: 10px">
          <span class="muted">清单文件</span>
          <b class="mono">{{ gateways.file_path }}</b>
        </div>
        <p class="hint">{{ gateways.disclaimer }}</p>
      </template>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>{{ editingId ? '改这一条' : '加一条网关' }}</h2>
        <span v-if="activeRow && !editingId" class="tag">当前「{{ activeRow.name }}」</span>
      </div>
      <label class="field">
        <span>名称</span>
        <input v-model="gatewayDraft.name" :maxlength="gateways?.name_max ?? 40" placeholder="例如 NAS 上那家" />
      </label>
      <label class="field" style="margin-top: 12px">
        <span>接口地址</span>
        <input v-model="gatewayDraft.base_url" placeholder="https://某个主机/v1" />
        <span v-if="editingId" class="hint">留空表示保持原地址。这一格不回显原值。</span>
      </label>
      <label class="field" style="margin-top: 12px">
        <span>模型</span>
        <input v-model="gatewayDraft.model" placeholder="deepseek-chat" />
      </label>
      <label class="field" style="margin-top: 12px">
        <span>API Key</span>
        <input v-model="gatewayDraft.api_key" type="password" autocomplete="new-password" placeholder="留空则不修改" />
        <span class="hint">只写入，接口不会把密钥返回来。</span>
      </label>
      <div class="toolbar" style="margin-top: 14px">
        <button class="btn" type="submit" :disabled="gatewayBusy">{{ editingId ? '保存这一条' : '加入清单' }}</button>
        <button v-if="editingId" class="btn btn-ghost" type="button" :disabled="gatewayBusy" @click="clearGatewayDraft">取消编辑</button>
        <button v-else class="btn btn-ghost" type="button" :disabled="gatewayBusy" @click="loadGateways">重新读取清单</button>
      </div>
    </section>
  </form>
</template>
