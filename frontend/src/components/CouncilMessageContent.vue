<script setup lang="ts">
import { computed } from 'vue'
import { messageBlocks, inlineParts } from '@/composables/councilChat'
const props=defineProps<{text:string}>()
const blocks=computed(()=>messageBlocks(props.text))
</script>
<template>
  <div class="message-content">
    <template v-for="(block,index) in blocks" :key="index">
      <pre v-if="block.kind==='code'" class="message-code"><small v-if="block.language">{{ block.language }}</small><code>{{ block.text }}</code></pre>
      <div v-else-if="block.kind==='table'" class="message-table" tabindex="0" aria-label="分析表格，可横向滚动"><table><thead><tr><th v-for="(cell,i) in block.head" :key="i">{{ cell }}</th></tr></thead><tbody><tr v-for="(row,r) in block.rows" :key="r"><td v-for="(cell,c) in row" :key="c">{{ cell }}</td></tr></tbody></table></div>
      <p v-else><template v-for="(part,i) in inlineParts(block.text)" :key="i"><a v-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a><template v-else>{{ part.text }}</template></template></p>
    </template>
  </div>
</template>
<style scoped>
.message-content {min-width:0;overflow-wrap:anywhere}.message-content p{white-space:pre-wrap;margin:0}.message-content a{color:var(--accent,#287aab);text-decoration:underline;word-break:break-all}.message-code{overflow-x:auto;max-width:100%;white-space:pre;background:#14202d;color:#eef4fc;padding:12px;border-radius:6px;font-size:12px}.message-code small{display:block;opacity:.65;margin-bottom:6px}.message-code code{font:inherit}.message-table{overflow-x:auto;max-width:100%;margin:8px 0}.message-table table{border-collapse:collapse;min-width:280px}.message-table th,.message-table td{border:1px solid #89939c55;padding:6px;white-space:pre-wrap;font-size:12px}
</style>
