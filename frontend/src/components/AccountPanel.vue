<script setup lang="ts">
import { request } from '@/api/http'
import { onMounted, reactive, ref } from 'vue'

interface Sess { id: string; ua: string; ip: string; created: number; last_seen: number; current: boolean }
const status = ref<{ required: boolean; username: string | null; from_env: boolean; headless: boolean } | null>(null)
const items = ref<Sess[]>([])
const form = reactive({ current_password: '', username: '', new_password: '', new_password2: '' })
const msg = ref<{ ok: boolean; text: string } | null>(null)

const fmt = (t: number) => (t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '—')
const device = (ua: string) => (/(iPhone|iPad|Android|Windows|Macintosh|Linux)/.exec(ua)?.[1] ?? '未知设备') + (/(Edg|Chrome|Firefox|Safari|wxwork|MicroMessenger)/.exec(ua)?.[1] ? ` · ${/(Edg|Chrome|Firefox|Safari|wxwork|MicroMessenger)/.exec(ua)?.[1]}` : '')

async function load() {
  status.value = await request('/api/auth/status')
  if (status.value?.required) {
    const r = await request<{ items: Sess[]; username: string | null }>('/api/auth/sessions')
    items.value = r.items
    form.username = r.username || ''
  }
}

async function save() {
  msg.value = null
  if (form.new_password && form.new_password !== form.new_password2) {
    msg.value = { ok: false, text: '两次输入的新密码不一样' }
    return
  }
  try {
    await request('/api/auth/change', { method: 'POST', body: JSON.stringify({ current_password: form.current_password, username: form.username, new_password: form.new_password }) })
    form.current_password = form.new_password = form.new_password2 = ''
    msg.value = { ok: true, text: '已保存。其他设备需要重新登录。' }
    await load()
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '保存失败' }
  }
}

async function logout(all = false) {
  await request(all ? '/api/auth/logout-all' : '/api/auth/logout', { method: 'POST', body: '{}' })
  window.location.reload()
}

onMounted(load)
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h2>账号与登录</h2>
      <span class="tag" :class="status?.required ? 'tag-hot' : 'type-weak'">{{ status?.required ? `已登录：${status.username}` : '本机桌面版，无需登录' }}</span>
    </div>
    <template v-if="!status?.required">
      <p class="hint">桌面版只在本机打开，不需要账号。Docker / 网页版会要求先创建管理员账号；也可以用环境变量 LP_USER / LP_PASSWORD 指定。</p>
    </template>
    <template v-else>
      <p v-if="status.from_env" class="hint">账号来自环境变量 LP_USER / LP_PASSWORD，要修改请改环境变量后重启容器。</p>
      <div v-else class="grid">
        <label class="field"><span>用户名</span><input v-model="form.username" autocomplete="username" /></label>
        <label class="field"><span>当前密码（必填）</span><input v-model="form.current_password" type="password" autocomplete="current-password" /></label>
        <label class="field"><span>新密码（不改留空）</span><input v-model="form.new_password" type="password" autocomplete="new-password" /></label>
        <label class="field"><span>再输一次新密码</span><input v-model="form.new_password2" type="password" autocomplete="new-password" /></label>
      </div>
      <div class="actions">
        <button v-if="!status.from_env" class="btn" type="button" :disabled="!form.current_password" @click="save">保存账号</button>
        <button class="btn btn-ghost" type="button" @click="logout(false)">退出</button>
        <button class="btn btn-ghost" type="button" @click="logout(true)">退出所有设备</button>
      </div>
      <p v-if="msg" class="hint" :style="{ color: msg.ok ? '#16a34a' : '#dc2626' }">{{ msg.text }}</p>
      <h3>已登录的设备（{{ items.length }}）</h3>
      <table class="sess">
        <tr v-for="s in items" :key="s.id"><td>{{ device(s.ua) }}{{ s.current ? '（本机）' : '' }}</td><td>{{ s.ip }}</td><td>登录 {{ fmt(s.created) }}</td><td>最近 {{ fmt(s.last_seen) }}</td></tr>
      </table>
    </template>
  </section>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin-top: 8px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
h3 { font-size: 14px; margin: 16px 0 6px; }
.sess { width: 100%; font-size: 13px; border-collapse: collapse; }
.sess td { padding: 4px 6px; border-top: 1px solid rgba(127, 127, 127, 0.2); }
</style>
