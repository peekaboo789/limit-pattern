<script setup lang="ts">
import { ApiError, request } from '@/api/http'
import { onMounted, onUnmounted, reactive, ref } from 'vue'

type View = Record<string, unknown>
const EVENT_LABELS: Record<string, string> = {
  alert: '盘中报警（AI 榜单 + 自选股，≥3% / ≥6%，每只每档每天一次）',
  ranking: 'AI 榜单生成 / 导入',
  summary: '每日总结完成',
  sync_failed: '日线同步失败',
  capture: '手动采集完成（前 10）',
}
const SECRETS = ['wechat_token'] as const

const view = ref<View | null>(null)
const form = reactive({
  wechat_enabled: false, wechat_api_base: 'https://ilinkai.weixin.qq.com',
  wechat_token: '', wechat_username: '', wechat_events: [] as string[],
})
const saving = ref(false)
const testing = ref(false)
const msg = ref<{ ok: boolean; text: string } | null>(null)
const qrcode = ref('')
const qrExpires = ref<number | null>(null)
const qrLoading = ref(false)
const loggedIn = ref(false)
const qrStatus = ref('')
const statusLabel = ref('')
const admin = ref<{ bound: boolean; masked: string; bound_at: string; user_id?: string } | null>(null)
const unbinding = ref(false)
let pollTimer: number | undefined

function fill(v: View) {
  view.value = v
  for (const k of Object.keys(form) as (keyof typeof form)[]) {
    if ((SECRETS as readonly string[]).includes(k)) (form as Record<string, unknown>)[k] = ''
    else if (k in v) (form as Record<string, unknown>)[k] = k === 'wechat_events' ? [...((v[k] as string[]) || [])] : v[k]
  }
}

async function load() {
  fill(await request<View>('/api/settings'))
  await refreshStatus()
}

async function refreshStatus() {
  try {
    const r = await request<{ logged_in: boolean; ready: boolean; status?: string; admin?: { bound: boolean; masked: string; bound_at: string; user_id?: string } }>('/api/wechat/status')
    loggedIn.value = !!r.logged_in
    if (r.admin) admin.value = r.admin
    if (r.logged_in) clearQr()
  } catch {
    loggedIn.value = false
  }
}

async function save() {
  saving.value = true
  msg.value = null
  try {
    const body: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(form)) {
      if ((SECRETS as readonly string[]).includes(k)) {
        if (String(v).trim()) body[k] = String(v).trim()
      } else body[k] = typeof v === 'string' ? v.trim() : v
    }
    fill(await request<View>('/api/settings', { method: 'PUT', body: JSON.stringify(body) }))
    msg.value = { ok: true, text: '个人微信设置已保存' }
    clearQr()
    await refreshStatus()
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '保存失败' }
  } finally {
    saving.value = false
  }
}

async function clearSecret(field: string) {
  fill(await request<View>('/api/settings', { method: 'PUT', body: JSON.stringify({ [`${field}_clear`]: true }) }))
}

async function getQr() {
  qrLoading.value = true
  msg.value = null
  qrStatus.value = ''
  try {
    const r = await request<{ qrcode: string; expires_seconds?: number | null; qrcode_img_content?: string }>('/api/wechat/qrcode')
    qrcode.value = r.qrcode || ''
    qrExpires.value = r.expires_seconds ? Date.now() + r.expires_seconds * 1000 : null
    msg.value = { ok: true, text: '二维码已生成，请用个人微信扫码登录。这个二维码图片不是直接加载的链接，而是程序按官方返回链接本地生成的。' }
    startPolling()
  } catch (e) {
    qrcode.value = ''
    msg.value = { ok: false, text: e instanceof ApiError ? e.detail : e instanceof Error ? e.message : '获取二维码失败' }
  } finally {
    qrLoading.value = false
  }
}

function clearQr() {
  qrcode.value = ''
  qrExpires.value = null
  qrStatus.value = ''
  statusLabel.value = ''
  stopPolling()
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    try {
      const r = await request<{ logged_in: boolean; status?: string; admin?: { bound: boolean; masked: string; bound_at: string; user_id?: string } }>('/api/wechat/status')
      loggedIn.value = !!r.logged_in
      if (r.admin) admin.value = r.admin
      if (r.logged_in) {
        msg.value = { ok: true, text: '扫码登录成功，已保存会话到 ./data。请先用自己的微信给机器人发任意一条消息，绑定为管理员。' }
        clearQr()
      } else {
        qrStatus.value = r.status || 'wait'
        statusLabel.value = statusText(qrStatus.value)
        if (qrExpires.value && Date.now() > qrExpires.value) {
          msg.value = { ok: false, text: '二维码已过期，请重新获取' }
          clearQr()
        }
      }
    } catch {
      // 状态轮询失败不打断用户；下次再试
    }
  }, 2500)
}

function statusText(status: string): string {
  if (status === 'wait') return '等待扫码'
  if (status === 'scaned') return '已扫码，待确认'
  if (status === 'confirmed') return '登录成功'
  if (status === 'expired') return '已过期'
  if (status) return status
  return ''
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
}

async function test() {
  testing.value = true
  msg.value = null
  try {
    const r = await request<{ ok: boolean; message: string }>('/api/wechat/test', { method: 'POST', body: '{}' }, 40000)
    msg.value = { ok: r.ok, text: r.message }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof ApiError ? e.detail : e instanceof Error ? e.message : '发送失败' }
  } finally {
    testing.value = false
  }
}

async function unbind() {
  unbinding.value = true
  msg.value = null
  try {
    const r = await request<{ ok: boolean; admin: { bound: boolean; masked: string; bound_at: string } }>('/api/wechat/unbind', { method: 'POST', body: '{}' })
    admin.value = r.admin
    msg.value = { ok: true, text: '已解除绑定。下一个给机器人发消息的人会自动绑定为管理员。' }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof ApiError ? e.detail : e instanceof Error ? e.message : '解除绑定失败' }
  } finally {
    unbinding.value = false
  }
}

onMounted(() => {
  load()
  pollTimer = undefined
})

onUnmounted(() => {
  stopPolling()
})
</script>

<template>
  <section class="card wechat">
    <div class="card-head">
      <h2>个人微信</h2>
      <span class="tag" :class="view?.wechat_enabled ? 'tag-hot' : 'type-weak'">{{ view?.wechat_enabled ? '已启用' : '未启用' }}</span>
      <span v-if="loggedIn" class="tag tag-ok">已登录</span>
    </div>
    <p class="hint">
      通过 iLink Bot / ClawBot 的扫码登录接入个人微信，功能与企业微信对等（推送、文本命令、纯文本回复）。
      Session 只持久化在 <code>./data/wechat_session.json</code>，不会写入日志或回显 token。
      若使用非官方 / 自托管网关，请在下方替换接口地址；这类路线存在账号受限风险，请自行评估。
    </p>
    <label class="check"><input v-model="form.wechat_enabled" type="checkbox" /> 启用个人微信</label>
    <div class="grid">
      <label class="field">
        <span>接口地址 / 网关</span>
        <input v-model="form.wechat_api_base" placeholder="https://ilinkai.weixin.qq.com" />
        <span class="hint">默认官方 iLink Bot；自托管网关只要兼容同一接口形状即可替换。</span>
      </label>
      <label class="field">
        <span>Token（可选，只写不回显）<em class="set" v-if="view?.wechat_token_set">已设置 <a href="#" @click.prevent="clearSecret('wechat_token')">清除</a></em></span>
        <input v-model="form.wechat_token" type="password" autocomplete="new-password" :placeholder="view?.wechat_token_set ? '已设置，留空不修改' : '扫码成功会自动写入，一般不用填'" />
      </label>
      <label class="field"><span>登录账号名（可选）</span><input v-model="form.wechat_username" placeholder="用于自托管网关，可空" /></label>
      <div class="field">
        <span>管理员绑定</span>
        <div v-if="admin?.bound" class="admin-box">
          <span class="result">{{ admin.masked }}</span>
          <span v-if="admin.bound_at" class="muted">绑定于 {{ admin.bound_at }}</span>
          <button class="btn btn-ghost" type="button" :disabled="unbinding" @click="unbind">{{ unbinding ? '解除中…' : '解除绑定' }}</button>
        </div>
        <span v-else class="hint result">未绑定。扫码登录后，用你自己的微信给机器人发任意一条消息即可绑定。</span>
      </div>
    </div>

    <h3>扫码登录</h3>
    <p class="hint">先启用并保存，再点击获取二维码；手机微信扫码后状态会自动轮询。二维码过期需重新获取。</p>
    <div class="qr-row">
      <button class="btn btn-ghost" type="button" :disabled="qrLoading || !view?.wechat_enabled" @click="getQr">{{ qrLoading ? '获取中…' : '获取登录二维码' }}</button>
      <span v-if="!view?.wechat_enabled" class="hint result">请先勾选「启用个人微信」并保存，再获取二维码。</span>
      <span v-else-if="loggedIn" class="hint result">✅ 当前已登录，可直接发送测试消息。</span>
      <span v-else-if="qrcode" class="hint result">请用个人微信扫码，二维码 {{ qrExpires ? `将在 ${Math.max(0, Math.round((qrExpires - Date.now()) / 1000))} 秒后过期` : '有效期内' }}</span>
      <span v-if="qrStatus && !loggedIn" class="tag type-weak">{{ statusLabel }}</span>
    </div>
    <div v-if="qrcode" class="qr-box">
      <img v-if="qrcode.startsWith('http')" :src="qrcode" alt="个人微信登录二维码" />
      <img v-else :src="qrcode" alt="个人微信登录二维码" />
    </div>

    <h3>推送哪些事件</h3>
    <label v-for="e in (view?.wechat_event_options as string[]) || Object.keys(EVENT_LABELS)" :key="e" class="check">
      <input v-model="form.wechat_events" type="checkbox" :value="e" /> {{ EVENT_LABELS[e] || e }}
    </label>
    <div class="actions">
      <button class="btn" type="button" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存个人微信设置' }}</button>
      <button class="btn btn-ghost" type="button" :disabled="testing || !loggedIn" @click="test">{{ testing ? '发送中…' : '发送测试消息' }}</button>
    </div>
    <p v-if="msg" class="hint result" :style="{ color: msg.ok ? '#16a34a' : '#dc2626' }">{{ msg.text }}</p>
  </section>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-top: 12px; }
.check { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
h3 { font-size: 14px; margin: 18px 0 6px; }
.set { font-style: normal; color: #16a34a; font-size: 12px; margin-left: 6px; }
.actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.result { word-break: break-all; }
code { background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
.tag-ok { background: #16a34a22; color: #16a34a; }
.qr-row { display: flex; align-items: center; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
.qr-box { margin-top: 12px; max-width: 260px; padding: 8px; border: 1px solid #e5e7eb; border-radius: 8px; }
.qr-box img { width: 100%; display: block; }
.admin-box { display: flex; align-items: center; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
.muted { color: #6b7280; font-size: 12px; }
</style>
