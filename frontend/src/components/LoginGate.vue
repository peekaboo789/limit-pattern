<script setup lang="ts">
import { request } from '@/api/http'
import { onBeforeUnmount, onMounted, ref } from 'vue'

interface AuthStatus { required: boolean; logged_in: boolean; setup_needed: boolean }
const state = ref<'checking' | 'login' | 'setup' | 'ok'>('checking')
const username = ref('')
const password = ref('')
const password2 = ref('')
const remember = ref(true)
const error = ref('')
const busy = ref(false)

async function check() {
  try {
    const s = await request<AuthStatus>('/api/auth/status')
    state.value = !s.required || s.logged_in ? 'ok' : s.setup_needed ? 'setup' : 'login'
  } catch {
    state.value = 'ok'
  }
}

async function submit() {
  error.value = ''
  if (state.value === 'setup' && password.value !== password2.value) {
    error.value = '两次输入的密码不一样'
    return
  }
  busy.value = true
  try {
    const path = state.value === 'setup' ? '/api/auth/setup' : '/api/auth/login'
    await request(path, { method: 'POST', body: JSON.stringify({ username: username.value.trim(), password: password.value, remember: remember.value }) })
    password.value = ''
    window.location.reload()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录失败'
  } finally {
    busy.value = false
  }
}

const onRequired = () => void check()
onMounted(() => {
  window.addEventListener('lp-login-required', onRequired)
  void check()
})
onBeforeUnmount(() => window.removeEventListener('lp-login-required', onRequired))
</script>

<template>
  <slot v-if="state === 'ok'" />
  <div v-else-if="state !== 'checking'" class="login-wrap">
    <form class="login-card" @submit.prevent="submit">
      <div class="brand">
        <div class="logo">涨</div>
        <div>
          <h1>三日涨停形态统计</h1>
          <p>{{ state === 'setup' ? '首次使用：创建管理员账号' : '请登录' }}</p>
        </div>
      </div>
      <label class="field">
        <span>用户名</span>
        <input v-model="username" autocomplete="username" autofocus placeholder="admin" />
      </label>
      <label class="field">
        <span>密码</span>
        <input v-model="password" type="password" :autocomplete="state === 'setup' ? 'new-password' : 'current-password'" />
      </label>
      <label v-if="state === 'setup'" class="field">
        <span>再输一次密码</span>
        <input v-model="password2" type="password" autocomplete="new-password" />
      </label>
      <label v-if="state === 'login'" class="remember"><input v-model="remember" type="checkbox" /> 记住我（这台设备以后不用再登录，改密码后失效）</label>
      <button class="btn submit" type="submit" :disabled="busy || !username.trim() || !password">
        {{ busy ? '请稍候…' : state === 'setup' ? '创建并登录' : '登录' }}
      </button>
      <p v-if="error" class="error">{{ error }}</p>
      <p class="foot">{{ state === 'setup' ? '密码只以加盐哈希保存在数据目录，至少 6 位。' : '连续输错 5 次会锁定 5 分钟。' }}</p>
    </form>
  </div>
</template>

<style scoped>
.login-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: radial-gradient(circle at 20% 10%, rgba(232, 185, 96, 0.18), transparent 40%), #0b0f17; padding: 20px; }
.login-card { width: 380px; max-width: 100%; background: #111827; border: 1px solid #1f2937; border-radius: 14px; padding: 28px 28px 20px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.45); display: flex; flex-direction: column; gap: 14px; color: #e5e7eb; }
.brand { display: flex; gap: 12px; align-items: center; margin-bottom: 6px; }
.logo { width: 44px; height: 44px; border-radius: 10px; background: linear-gradient(135deg, #f0c27b, #d69e2e); color: #1f1300;
  font-weight: 800; font-size: 22px; display: flex; align-items: center; justify-content: center; }
h1 { font-size: 18px; margin: 0; }
.brand p { margin: 2px 0 0; color: #9ca3af; font-size: 13px; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: #9ca3af; }
.field input { background: #0b1220; border: 1px solid #263244; border-radius: 8px; padding: 10px 12px; color: #f3f4f6; font-size: 14px; }
.field input:focus { outline: none; border-color: #e8b960; }
.remember { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #cbd5e1; }
.submit { width: 100%; padding: 10px; font-size: 15px; margin-top: 4px; }
.error { color: #f87171; font-size: 13px; margin: 0; }
.foot { color: #6b7280; font-size: 12px; margin: 0; text-align: center; }
</style>
