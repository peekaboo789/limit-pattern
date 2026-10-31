<script setup lang="ts">
import { request } from '@/api/http'
import { onBeforeUnmount, onMounted, ref } from 'vue'

interface Info { docker_inapp?: boolean; current: string; latest: string | null; newer: boolean; mode: string; notes?: string; url?: string; error?: string; can_auto?: boolean; docker_command: string; published_at?: string }
interface St { mode?: string; supervised?: boolean; version?: string | null; phase: string; message: string; downloaded: number; total: number; error: string }

const info = ref<Info | null>(null)
const st = ref<St | null>(null)
const checking = ref(false)
const err = ref('')
const mirror = ref('')
let timer = 0

async function check(force = true) {
  checking.value = true
  err.value = ''
  try {
    info.value = await request<Info>(`/api/update/check?force=${force}`, undefined, 40000)
  } catch (e) {
    err.value = e instanceof Error ? e.message : '检查失败'
  } finally {
    checking.value = false
  }
}

async function poll() {
  try {
    st.value = await request<St>('/api/update/status')
  } catch {
    // Docker 程序内更新：重启期间接口暂时连不上，继续等
    if (st.value?.phase === 'applying') timer = window.setTimeout(poll, 2000)
    return
  }
  if (['download', 'verify', 'extract', 'applying'].includes(st.value.phase)) timer = window.setTimeout(poll, st.value.phase === 'applying' ? 2000 : 800)
  if (st.value.phase === 'done' && wasApplying) window.setTimeout(() => window.location.reload(), 1500)
  if (st.value.phase === 'applying') wasApplying = true
}
let wasApplying = false

async function download() {
  err.value = ''
  try {
    st.value = await request<St>('/api/update/download', { method: 'POST', body: '{}' })
    void poll()
  } catch (e) {
    err.value = e instanceof Error ? e.message : '下载失败'
  }
}

async function apply() {
  const docker = info.value?.mode === 'docker'
  if (docker && !window.confirm(`确认更新到 v${info.value?.latest ?? st.value?.version}？\n\n会先备份数据到 backups/，然后程序自动重启（约半分钟页面不可用）。数据卷 /data 里的数据全部保留，失败会自动回退到当前版本。`)) return
  try {
    st.value = await request<St>('/api/update/apply', { method: 'POST', body: JSON.stringify({ confirm: true }) })
    wasApplying = true
    timer = window.setTimeout(poll, 3000)
  } catch (e) {
    err.value = e instanceof Error ? e.message : '更新失败'
  }
}

async function saveMirror() {
  await request('/api/settings', { method: 'PUT', body: JSON.stringify({ update_mirror: mirror.value.trim() }) })
}

function copy(text: string) {
  void navigator.clipboard?.writeText(text)
}

const pct = () => (st.value && st.value.total ? Math.round((st.value.downloaded / st.value.total) * 100) : 0)

onMounted(async () => {
  const s = await request<Record<string, unknown>>('/api/settings')
  mirror.value = String(s.update_mirror || '')
  void poll()
})
onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h2>在线更新</h2>
      <span class="tag" :class="info?.newer ? 'tag-hot' : 'type-weak'">当前 v{{ info?.current ?? '…' }}</span>
    </div>
    <p class="hint">从 GitHub Releases 检查新版本。更新前自动把设置、数据库、自选股备份到数据目录 backups/（保留最近 3 份），程序替换不碰数据目录。</p>
    <div class="actions">
      <button class="btn btn-ghost" type="button" :disabled="checking" @click="check(true)">{{ checking ? '检查中…' : '检查更新' }}</button>
      <template v-if="(info?.newer && (info.mode === 'desktop' || info.docker_inapp)) || st?.phase === 'ready'">
        <button class="btn" type="button" :disabled="!info?.can_auto || ['download','verify','extract'].includes(st?.phase || '')" @click="download">下载 v{{ info?.latest ?? st?.version }}</button>
        <button v-if="st?.phase === 'ready'" class="btn" type="button" @click="apply">{{ info?.mode === 'docker' ? '一键更新（重启容器内程序）' : '立即更新并重启' }}</button>
      </template>
    </div>
    <p v-if="err" class="hint" style="color: #dc2626">{{ err }}</p>
    <p v-if="info?.error" class="hint" style="color: #dc2626">{{ info.error }}</p>
    <template v-if="info && info.latest">
      <p class="hint" :style="{ color: info.newer ? '#d97706' : '#16a34a' }">
        {{ info.newer ? `有新版本 v${info.latest}` : `已是最新版（最新 v${info.latest}）` }}
        <a v-if="info.url" :href="info.url" target="_blank" rel="noopener">发布页</a>
      </p>
      <pre v-if="info.newer && info.notes" class="notes">{{ info.notes }}</pre>
      <p v-if="info.newer && info.docker_inapp" class="hint">Docker 版程序内更新：只替换程序代码（存在 /data/app），不动镜像和数据；以后 docker compose pull 到更新的镜像会自动接管。新版本若需要新的 Python 依赖，会提示改用下面的命令。</p>
      <div v-if="info.newer && info.mode === 'docker'" class="docker">
        <p class="hint">{{ info.docker_inapp ? '也可以' : '这个镜像还不支持程序内更新，请' }}在服务器 docker-compose.yml 所在目录执行（/data 里的数据会保留）：</p>
        <code>{{ info.docker_command }}</code>
        <button class="btn btn-ghost" type="button" @click="copy(info.docker_command)">复制</button>
        <p class="hint">想自动更新可以加 Watchtower（见 docs/docker.md）。</p>
      </div>
      <p v-if="info.newer && info.mode === 'source'" class="hint">源码运行：git pull 后重启即可。</p>
    </template>
    <div v-if="st && st.phase !== 'idle'" style="margin-top: 8px">
      <progress v-if="st.phase === 'applying'" style="width: 100%"></progress>
      <progress v-if="st.phase === 'download'" :value="pct()" max="100" style="width: 100%"></progress>
      <p class="hint" :style="{ color: st.phase === 'error' ? '#dc2626' : undefined }">{{ st.message }}{{ st.phase === 'download' ? ` ${pct()}%` : '' }}</p>
    </div>
    <label v-if="info?.mode !== 'docker'" class="field" style="margin-top: 12px">
      <span>下载镜像前缀（可选）</span>
      <span style="display: flex; gap: 8px"><input v-model="mirror" placeholder="GitHub 下载慢时填加速前缀，如 https://ghfast.top" /><button class="btn btn-ghost" type="button" @click="saveMirror">保存</button></span>
      <span class="hint">先直连 GitHub，失败再走镜像；文件一律校验 sha256，镜像改不了内容。</span>
    </label>
  </section>
</template>

<style scoped>
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.notes { white-space: pre-wrap; background: rgba(127, 127, 127, 0.1); padding: 8px 10px; border-radius: 6px; max-height: 240px; overflow: auto; font-size: 13px; }
.docker code { display: inline-block; background: rgba(127, 127, 127, 0.15); padding: 4px 8px; border-radius: 4px; margin-right: 8px; }
</style>
