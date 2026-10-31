<script setup lang="ts">
import { request } from '@/api/http'
import { onBeforeUnmount, onMounted, ref } from 'vue'

const state = ref<'checking' | 'login' | 'ok'>('checking')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function check() {
  try {
    const s = await request<{ required: boolean; logged_in: boolean }>('/api/auth/status')
    state.value = s.required && !s.logged_in ? 'login' : 'ok'
  } catch {
    state.value = 'ok'
  }
}

async function login() {
  busy.value = true
  error.value = ''
  try {
    await request('/api/auth/login', { method: 'POST', body: JSON.stringify({ password: password.value }) })
    password.value = ''
    window.location.reload()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录失败'
  } finally {
    busy.value = false
  }
}

const onRequired = () => (state.value = 'login')
onMounted(() => {
  window.addEventListener('lp-login-required', onRequired)
  void check()
})
onBeforeUnmount(() => window.removeEventListener('lp-login-required', onRequired))
</script>

<template>
  <slot v-if="state === 'ok'" />
  <div v-else-if="state === 'login'" class="login-wrap">
    <form class="card login-card" @submit.prevent="login">
      <h2>三日涨停形态统计</h2>
      <p class="hint">这个服务设置了访问密码。</p>
      <label class="field">
        <span>访问密码</span>
        <input v-model="password" type="password" autocomplete="current-password" autofocus />
      </label>
      <button class="btn" type="submit" :disabled="busy || !password" style="margin-top: 12px; width: 100%">{{ busy ? '登录中…' : '登录' }}</button>
      <p v-if="error" class="hint" style="color: #dc2626">{{ error }}</p>
    </form>
  </div>
</template>

<style scoped>
.login-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #f5f6f8; }
.login-card { width: 340px; }
</style>
