<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/http'
interface Row { id: string; enabled: boolean; priority: number; provider_name: string; category: string; cache_ttl: number; timeout_seconds: number; min_interval: number; health: { status?: string; stock_code?: string; eval_date?: string; checked_at?: string; reason?: string; elapsed_seconds?: number; open_until?: number } }
interface Registry { version: string; builtin_version: string; schema_version: number; origin: string; content_sha256: string; rollback_available: boolean; candidate_version: string | null; items: Row[]; policy: string; load_error: string }
const registry = ref<Registry | null>(null), busy = ref(false), error = ref(''), notice = ref('')
const time = (v?: string) => v ? new Date(v).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) + ' 北京时间' : '尚未验证'
function status(row: Row) {
  if ((row.health.open_until || 0) > Date.now() / 1000) return '熔断冷却中'
  return ({ ok: '已取到', empty: '有效查询无记录', failed: '接口失败', unverified: '未核验', not_applicable: '不适用', unavailable: '不可用 / 已降级' } as Record<string, string>)[row.health.status || ''] || '尚未验证'
}
async function run(path = '', method = 'GET', body?: object) {
  busy.value = true; error.value = ''; notice.value = ''
  try {
    registry.value = await request<Registry>('/api/council/sources' + path, { method, ...(body ? { body: JSON.stringify(body) } : {}) }, 12000)
    notice.value = path === '/check-update' ? registry.value.candidate_version ? `已验证清单 ${registry.value.candidate_version}，点击应用生效。` : '没有更高版本，保留当前配置。' : method === 'GET' ? '' : '已原子保存，仅影响下一次讨论采集，不改已有会话证据。'
  } catch (e) { error.value = (e instanceof Error ? e.message : String(e)) + '；当前清单保留，断网可继续使用内置/上次版本。' }
  finally { busy.value = false }
}
onMounted(() => run())
</script>
<template>
  <details class="source-panel">
    <summary>免费公开数据源管理 <span v-if="registry">· 清单 {{ registry.version }} · {{ registry.items.filter(s => s.enabled).length }}/{{ registry.items.length }} 启用</span></summary>
    <p class="hint">东方财富、新浪、腾讯、同花顺、巨潮及交易所公开渠道。同站多个接口不算多个独立来源；免费访问不代表稳定性、商业授权或覆盖完整保证。</p>
    <div class="source-actions">
      <button class="btn" :disabled="busy" @click="run()">刷新健康状态</button>
      <button class="btn" :disabled="busy" @click="run('/check-update', 'POST')">检查清单更新</button>
      <button v-if="registry?.candidate_version" class="btn" :disabled="busy" @click="run('/apply-update', 'POST')">应用 {{ registry.candidate_version }}</button>
      <button class="btn" :disabled="busy || !registry?.rollback_available" @click="run('/rollback', 'POST')">回滚上一清单</button>
      <span v-if="busy" class="hint">处理中…</span>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" class="hint" role="status">{{ notice }}</p>
    <template v-if="registry">
      <p v-if="registry.load_error" class="error">{{ registry.load_error }}</p>
      <p class="hint">{{ registry.policy }} 内置 {{ registry.builtin_version }}；schema {{ registry.schema_version }}。个人启用/排序设置优先。健康来自实际采集，完成讨论后可刷新；全部关闭会触发证据不足。</p>
      <div class="source-table-wrap"><table>
        <thead><tr><th>启用</th><th>来源 / 覆盖</th><th>优先级（小值先）</th><th>最近状态</th><th>预算 / 缓存</th></tr></thead>
        <tbody><tr v-for="row in registry.items" :key="row.id">
          <td><input type="checkbox" :checked="row.enabled" :disabled="busy" :aria-label="'启用 ' + row.id" @change="run('/' + row.id, 'PUT', { enabled: ($event.target as HTMLInputElement).checked })"></td>
          <td><b>{{ row.provider_name }}</b> · {{ row.category }}<br><small>{{ row.id }}</small></td>
          <td><input type="number" min="1" max="100" :value="row.priority" :disabled="busy" :aria-label="row.id + ' 优先级'" @change="run('/' + row.id, 'PUT', { priority: Number(($event.target as HTMLInputElement).value) })"></td>
          <td><b>{{ status(row) }}</b> <small>{{ row.health.stock_code }} {{ row.health.eval_date }}</small><br><small>{{ time(row.health.checked_at) }}<template v-if="row.health.elapsed_seconds != null"> · {{ row.health.elapsed_seconds }}s</template></small><details v-if="row.health.reason"><summary>原因</summary><small>{{ row.health.reason }}</small></details></td>
          <td>{{ row.timeout_seconds }}s / {{ row.cache_ttl }}s缓存<br><small>间隔≥{{ row.min_interval }}s</small></td>
        </tr></tbody>
      </table></div>
      <p class="hint">四并发、采集总预算32秒、瞬态重试最多2次、连续3次故障熔断60秒；无记录/不适用不计故障。跨站冲突会标记，缺失不补零；新解析器或地址模板须正常软件更新。</p>
      <details><summary>可信来源与完整性</summary><p class="hint">仅从 {{ registry.origin }} 固定HTTPS地址手动下载JSON，严格schema、64KiB、兼容版本及SHA256校验；不执行远端Python/JS，不接受自定义URL/凭据/私网。SHA256用于完整性，不宣称独立数字签名。</p><code>{{ registry.content_sha256 }}</code></details>
    </template>
  </details>
</template>
<style scoped>
.source-panel { margin: 16px 0; padding: 12px; border: 1px solid rgba(127,127,127,.3); border-radius: 8px; }
summary { cursor: pointer; }.source-actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
.source-table-wrap { overflow: auto; max-height: 420px; }table { width: 100%; border-collapse: collapse; font-size: 12px; }
th,td { text-align: left; padding: 7px; border-bottom: 1px solid rgba(127,127,127,.2); vertical-align: top; }input[type=number] { width: 64px; }
small,.hint { opacity: .8; overflow-wrap: anywhere; }.hint { font-size: 12px; line-height: 1.6; }.error { color: #dc5c5c; }code { font-size: 11px; overflow-wrap: anywhere; }
</style>
