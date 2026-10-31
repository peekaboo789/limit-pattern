<script setup lang="ts">
import { request } from '@/api/http'
import { onMounted, reactive, ref } from 'vue'

interface Prof { id: string; name: string; base_url: string; model: string; api_key_set: boolean; temperature: number | null; max_tokens: number | null; active: boolean }
const items = ref<Prof[]>([])
const editing = ref<string | null>(null) // id，'new' = 新增
const form = reactive({ name: '', base_url: '', model: '', api_key: '', temperature: '' as string | number, max_tokens: '' as string | number })
const msg = ref<{ ok: boolean; text: string } | null>(null)
const testing = ref<string | null>(null)

async function load() {
  items.value = (await request<{ items: Prof[] }>('/api/ai/profiles')).items
}

function edit(p?: Prof) {
  editing.value = p ? p.id : 'new'
  Object.assign(form, { name: p?.name ?? '', base_url: p?.base_url ?? '', model: p?.model ?? '', api_key: '', temperature: p?.temperature ?? '', max_tokens: p?.max_tokens ?? '' })
  msg.value = null
}

function body() {
  return { name: form.name, base_url: form.base_url, model: form.model, api_key: form.api_key || undefined,
    temperature: form.temperature === '' ? null : Number(form.temperature), max_tokens: form.max_tokens === '' ? null : Number(form.max_tokens) }
}

async function save() {
  try {
    const path = editing.value === 'new' ? '/api/ai/profiles' : `/api/ai/profiles/${editing.value}`
    const r = await request<{ items: Prof[] }>(path, { method: editing.value === 'new' ? 'POST' : 'PUT', body: JSON.stringify(body()) })
    items.value = r.items
    editing.value = null
    msg.value = { ok: true, text: '已保存' }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '保存失败' }
  }
}

async function activate(p: Prof) {
  items.value = (await request<{ items: Prof[] }>(`/api/ai/profiles/${p.id}/activate`, { method: 'POST', body: '{}' })).items
  msg.value = { ok: true, text: `已切换到 ${p.name}，AI 榜单和每日总结立即使用它` }
}

async function remove(p: Prof) {
  if (!window.confirm(`删除模型配置「${p.name}」？`)) return
  items.value = (await request<{ items: Prof[] }>(`/api/ai/profiles/${p.id}`, { method: 'DELETE' })).items
}

async function test(p?: Prof) {
  const id = p?.id ?? (editing.value === 'new' ? '' : editing.value)
  testing.value = id || 'new'
  msg.value = null
  try {
    const payload = p ? { id } : { id, ...body() }
    const r = await request<{ ok: boolean; reply?: string; error?: string; latency_ms: number; endpoint: string; scoring_ok?: boolean; scoring_ms?: number; scoring_error?: string; scoring_count?: number }>('/api/ai/profiles/test', { method: 'POST', body: JSON.stringify(payload) }, 200000)
    msg.value = r.ok ? { ok: r.scoring_ok !== false, text: `连接成功（${r.latency_ms} ms）：${r.reply}；试打分 2 只：` + (r.scoring_ok ? `成功（${r.scoring_ms} ms，解析出 ${r.scoring_count} 只${r.scoring_error ? '，' + r.scoring_error : ''}）` : `失败（${r.scoring_error}）`) } : { ok: false, text: `${r.error}（${r.endpoint}）` }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '测试失败' }
  } finally {
    testing.value = null
  }
}

onMounted(load)
</script>

<template>
  <section class="card ai-models">
    <div class="card-head">
      <h2>AI 模型</h2>
      <button class="btn btn-ghost" type="button" @click="edit()">+ 添加模型</button>
    </div>
    <p class="hint">可保存多个 OpenAI 兼容接口的模型，一键切换当前使用的（AI 榜单、每日总结、测试都用当前模型）。API Key 只写入、不回显。</p>
    <table v-if="items.length" class="models">
      <thead><tr><th>名称</th><th>模型</th><th>接口地址</th><th>密钥</th><th>参数</th><th></th></tr></thead>
      <tbody>
        <tr v-for="p in items" :key="p.id" :class="{ current: p.active }">
          <td><b>{{ p.name }}</b> <span v-if="p.active" class="tag tag-hot">当前</span></td>
          <td>{{ p.model || '—' }}</td>
          <td class="url">{{ p.base_url || '—' }}</td>
          <td>{{ p.api_key_set ? '已设置' : '未设置' }}</td>
          <td class="muted">{{ p.temperature ?? '默认' }} / {{ p.max_tokens ?? '默认' }}</td>
          <td class="ops">
            <button v-if="!p.active" class="btn" type="button" @click="activate(p)">设为当前</button>
            <button class="btn btn-ghost" type="button" :disabled="testing === p.id" @click="test(p)">{{ testing === p.id ? '测试中…' : '测试' }}</button>
            <button class="btn btn-ghost" type="button" @click="edit(p)">编辑</button>
            <button class="btn btn-ghost" type="button" @click="remove(p)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="hint">还没有模型配置，点「添加模型」。</p>
    <div v-if="editing" class="editor">
      <h3>{{ editing === 'new' ? '添加模型' : '编辑模型' }}</h3>
      <div class="grid">
        <label class="field"><span>名称</span><input v-model="form.name" placeholder="例如 DeepSeek / 通义千问" /></label>
        <label class="field"><span>接口地址（OpenAI 兼容，到 /v1）</span><input v-model="form.base_url" placeholder="https://api.deepseek.com/v1" /></label>
        <label class="field"><span>模型 ID</span><input v-model="form.model" placeholder="deepseek-chat" /></label>
        <label class="field"><span>API Key</span><input v-model="form.api_key" type="password" autocomplete="new-password" :placeholder="editing === 'new' ? 'sk-…' : '留空则不修改'" /></label>
        <label class="field"><span>temperature（可选，0–2）</span><input v-model="form.temperature" type="number" step="0.1" min="0" max="2" placeholder="默认 0.2" /></label>
        <label class="field"><span>max_tokens（可选）</span><input v-model="form.max_tokens" type="number" min="16" placeholder="默认不限制" /></label>
      </div>
      <div class="actions">
        <button class="btn" type="button" @click="save">保存</button>
        <button class="btn btn-ghost" type="button" :disabled="testing !== null" @click="test()">{{ testing === 'new' || testing === editing ? '测试中…' : '测试连接（用表单里的值）' }}</button>
        <button class="btn btn-ghost" type="button" @click="editing = null">取消</button>
      </div>
    </div>
    <p v-if="msg" class="hint" :style="{ color: msg.ok ? '#16a34a' : '#dc2626', wordBreak: 'break-all' }">{{ msg.text }}</p>
  </section>
</template>

<style scoped>
.models { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.models th, .models td { text-align: left; padding: 6px 8px; border-top: 1px solid rgba(127, 127, 127, 0.2); }
.models tr.current td { background: rgba(232, 185, 96, 0.08); }
.url { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops { display: flex; gap: 6px; flex-wrap: wrap; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.editor { margin-top: 14px; padding-top: 6px; border-top: 1px dashed rgba(127, 127, 127, 0.3); }
h3 { font-size: 14px; margin: 8px 0; }
.actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
</style>
