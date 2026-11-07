<script setup lang="ts">
import { request } from '@/api/http'
import { onActivated, onMounted, ref } from 'vue'

interface Prof { id: string; name: string; model: string; api_key_set: boolean; active: boolean }
const items = ref<Prof[]>([])
const active = ref<string | null>(null)
const busy = ref(false)
const note = ref('')

async function load() {
  try {
    const r = await request<{ items: Prof[]; active: string | null }>('/api/ai/profiles')
    items.value = r.items
    active.value = r.active
  } catch {
    items.value = []
  }
}

async function change(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  busy.value = true
  try {
    const r = await request<{ items: Prof[]; active: string | null; active_name: string | null }>(`/api/ai/profiles/${id}/activate`, { method: 'POST', body: '{}' })
    items.value = r.items
    active.value = r.active
    note.value = `已切换到 ${r.active_name}`
    window.setTimeout(() => (note.value = ''), 3000)
  } finally {
    busy.value = false
  }
}

onMounted(load)
onActivated(load)
</script>

<template>
  <span v-if="items.length" class="switcher" title="当前用于 AI 榜单 / 每日总结的模型，切换立即生效">
    <span class="muted">AI 模型</span>
    <select :value="active ?? ''" :disabled="busy" @change="change">
      <option v-for="p in items" :key="p.id" :value="p.id">{{ p.name }}（{{ p.model || '未填模型' }}）{{ p.api_key_set ? '' : ' 缺密钥' }}</option>
    </select>
    <span v-if="note" class="ok">{{ note }}</span>
  </span>
  <router-link v-else to="/settings" class="muted">未配置 AI 模型</router-link>
</template>

<style scoped>
.switcher { display: inline-flex; align-items: center; gap: 6px; }
.ok { color: #16a34a; font-size: 12px; }
</style>
