<script setup lang="ts">
import { request } from '@/api/http'
import { askBrowserNotify } from '@/stores/monitor'
import { onMounted, reactive, ref } from 'vue'

type View = Record<string, unknown>
const EVENT_LABELS: Record<string, string> = {
  alert: '盘中报警（AI 榜单 + 自选股，≥3% / ≥6%，每只每档每天一次）',
  ranking: 'AI 榜单生成 / 导入',
  summary: '每日总结完成',
  sync_failed: '日线同步失败',
  capture: '手动采集完成（前 10）',
}
const SECRETS = ['wecom_secret', 'wecom_token', 'wecom_aes_key', 'wecom_webhook'] as const

const view = ref<View | null>(null)
const form = reactive({
  wecom_enabled: false, wecom_corpid: '', wecom_agentid: '', wecom_proxy: 'https://qyapi.weixin.qq.com',
  wecom_touser: '@all', wecom_toparty: '', wecom_totag: '', wecom_admins: '', wecom_events: [] as string[],
  wecom_secret: '', wecom_token: '', wecom_aes_key: '', wecom_webhook: '', access_password: '',
})
const saving = ref(false)
const testing = ref(false)
const msg = ref<{ ok: boolean; text: string } | null>(null)
const notifyState = ref(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission)
const callbackUrl = `${window.location.origin}/api/wecom/callback`

function fill(v: View) {
  view.value = v
  for (const k of Object.keys(form) as (keyof typeof form)[]) {
    if ((SECRETS as readonly string[]).includes(k) || k === 'access_password') (form as Record<string, unknown>)[k] = ''
    else if (k in v) (form as Record<string, unknown>)[k] = k === 'wecom_events' ? [...((v[k] as string[]) || [])] : v[k]
  }
}

async function load() {
  fill(await request<View>('/api/settings'))
}

async function save() {
  saving.value = true
  msg.value = null
  try {
    const body: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(form)) {
      if ((SECRETS as readonly string[]).includes(k) || k === 'access_password') {
        if (String(v).trim()) body[k] = String(v).trim() // 留空 = 不修改
      } else body[k] = typeof v === 'string' ? v.trim() : v
    }
    fill(await request<View>('/api/settings', { method: 'PUT', body: JSON.stringify(body) }))
    msg.value = { ok: true, text: '企业微信设置已保存' }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '保存失败' }
  } finally {
    saving.value = false
  }
}

async function clearSecret(field: string) {
  fill(await request<View>('/api/settings', { method: 'PUT', body: JSON.stringify({ [`${field}_clear`]: true }) }))
}

async function test() {
  testing.value = true
  msg.value = null
  try {
    const r = await request<{ ok: boolean; message: string; errcode: number | null }>('/api/wecom/test', { method: 'POST', body: '{}' }, 40000)
    msg.value = { ok: r.ok, text: r.message }
  } catch (e) {
    msg.value = { ok: false, text: e instanceof Error ? e.message : '发送失败' }
  } finally {
    testing.value = false
  }
}

async function allowBrowser() {
  notifyState.value = await askBrowserNotify()
}

onMounted(load)
</script>

<template>
  <section class="card wecom">
    <div class="card-head">
      <h2>企业微信</h2>
      <span class="tag" :class="view?.wecom_enabled ? 'tag-hot' : 'type-weak'">{{ view?.wecom_enabled ? '已启用' : '未启用' }}</span>
    </div>
    <p class="hint">用企业微信「自建应用」推送提醒，并可在企业微信里给应用发指令（榜单 / 自选 / 加自选 X / 删自选 X / 采集 / 总结 / 帮助）。Secret、Token、EncodingAESKey 只写入、不回显。</p>
    <label class="check"><input v-model="form.wecom_enabled" type="checkbox" /> 启用企业微信推送</label>
    <div class="grid">
      <label class="field"><span>企业ID（corpid）</span><input v-model="form.wecom_corpid" placeholder="ww开头，管理后台 我的企业 最下方" /></label>
      <label class="field"><span>AgentId</span><input v-model="form.wecom_agentid" placeholder="应用详情页的 AgentId" inputmode="numeric" /></label>
      <label class="field">
        <span>Secret <em class="set" v-if="view?.wecom_secret_set">已设置 <a href="#" @click.prevent="clearSecret('wecom_secret')">清除</a></em></span>
        <input v-model="form.wecom_secret" type="password" autocomplete="new-password" :placeholder="view?.wecom_secret_set ? '已设置，留空不修改' : '应用详情页的 Secret'" />
      </label>
      <label class="field"><span>接口地址 / 代理</span><input v-model="form.wecom_proxy" placeholder="https://qyapi.weixin.qq.com" />
        <span class="hint">报 60020（IP 不在可信 IP）时，填一台已加入「企业可信IP」的服务器上的反向代理地址。只影响发出去的请求。</span></label>
      <label class="field"><span>接收人 touser</span><input v-model="form.wecom_touser" placeholder="@all 或 zhangsan|lisi" /></label>
      <label class="field"><span>部门 toparty / 标签 totag</span>
        <span style="display: flex; gap: 8px"><input v-model="form.wecom_toparty" placeholder="部门ID，可空" /><input v-model="form.wecom_totag" placeholder="标签ID，可空" /></span></label>
    </div>
    <h3>接收消息（回调）</h3>
    <p class="hint">需要公网 HTTPS 地址。在应用 → 接收消息 → 设置API接收 中填 URL：<code>{{ callbackUrl }}</code>，再把那里生成的 Token 和 EncodingAESKey 填到下面。桌面版一般没有公网地址，只能推送不能收消息。</p>
    <div class="grid">
      <label class="field">
        <span>Token <em class="set" v-if="view?.wecom_token_set">已设置 <a href="#" @click.prevent="clearSecret('wecom_token')">清除</a></em></span>
        <input v-model="form.wecom_token" type="password" autocomplete="new-password" :placeholder="view?.wecom_token_set ? '已设置，留空不修改' : '回调 Token'" />
      </label>
      <label class="field">
        <span>EncodingAESKey <em class="set" v-if="view?.wecom_aes_key_set">已设置 <a href="#" @click.prevent="clearSecret('wecom_aes_key')">清除</a></em></span>
        <input v-model="form.wecom_aes_key" type="password" autocomplete="new-password" :placeholder="view?.wecom_aes_key_set ? '已设置，留空不修改' : '43 位'" />
      </label>
      <label class="field"><span>指令白名单（成员账号）</span><input v-model="form.wecom_admins" placeholder="zhangsan,lisi（逗号分隔，只有他们能发指令）" /></label>
      <label class="field">
        <span>群机器人 webhook（可选）<em class="set" v-if="view?.wecom_webhook_set">已设置 <a href="#" @click.prevent="clearSecret('wecom_webhook')">清除</a></em></span>
        <input v-model="form.wecom_webhook" type="password" autocomplete="new-password" placeholder="更简单的推送通道，不需要可信IP；不能收指令" />
      </label>
    </div>
    <h3>推送哪些事件</h3>
    <label v-for="e in (view?.wecom_event_options as string[]) || Object.keys(EVENT_LABELS)" :key="e" class="check">
      <input v-model="form.wecom_events" type="checkbox" :value="e" /> {{ EVENT_LABELS[e] || e }}
    </label>
    <h3>网页访问密码（Docker）</h3>
    <label class="field">
      <span>访问密码 <em class="set" v-if="view?.access_password_set">已设置{{ view?.access_password_from_env ? '（来自环境变量 LP_PASSWORD）' : '' }}
        <a v-if="!view?.access_password_from_env" href="#" @click.prevent="clearSecret('access_password')">清除</a></em></span>
      <input v-model="form.access_password" type="password" autocomplete="new-password" placeholder="留空不修改；至少 4 位" />
      <span class="hint">只在 Docker / 网页版生效（桌面版只监听本机，不需要）。设置后打开页面要先登录。</span>
    </label>
    <div class="actions">
      <button class="btn" type="button" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存企业微信设置' }}</button>
      <button class="btn btn-ghost" type="button" :disabled="testing" @click="test">{{ testing ? '发送中…' : '发送测试消息' }}</button>
      <button v-if="notifyState !== 'unsupported'" class="btn btn-ghost" type="button" @click="allowBrowser">
        {{ notifyState === 'granted' ? '浏览器通知已允许' : '允许浏览器通知' }}</button>
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
</style>
