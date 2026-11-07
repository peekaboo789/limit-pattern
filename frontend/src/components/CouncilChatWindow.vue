<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, useId } from 'vue'
import { councilWindowBounds, councilWindowInsets, useCouncilWindow } from '@/composables/councilWindow'
const props=defineProps<{title:string;detailsReady?:boolean}>()
const emit=defineEmits<{close:[];'before-resize':[];resized:[]}>()
const {enlarged,details,toggleSize,escape}=useCouncilWindow()
const root=ref<HTMLElement|null>(null),viewport=ref({width:1280,height:800,right:0,bottom:0})
const titleId=useId()
const bounds=computed(()=>councilWindowBounds(viewport.value.width,viewport.value.height,enlarged.value))
let opener:HTMLElement|null=null,active=true
async function resize() {emit('before-resize');viewport.value={width:window.visualViewport?.width || window.innerWidth,height:window.visualViewport?.height || window.innerHeight,...councilWindowInsets(window.innerWidth,window.innerHeight,window.visualViewport)};await nextTick();if(active)emit('resized')}
async function toggle() {emit('before-resize');toggleSize();await nextTick();if(active)emit('resized')}
async function openDetails(){if(props.detailsReady===false)return;details.value=true;await nextTick();root.value?.querySelector<HTMLElement>('.window-details button')?.focus({preventScroll:true})}
async function closeDetails(){details.value=false;await nextTick();root.value?.querySelector<HTMLElement>('[data-details-toggle]')?.focus({preventScroll:true})}
function keydown(event:KeyboardEvent) {
  if(event.key!=='Escape'||event.defaultPrevented||event.target instanceof HTMLSelectElement)return
  const action=escape(event.isComposing||event.keyCode===229)
  if(action==='none')return
  event.preventDefault();event.stopPropagation()
  if(action==='close')emit('close');else void closeDetails()
}
onMounted(()=>{opener=document.activeElement instanceof HTMLElement?document.activeElement:null;void resize();root.value?.focus({preventScroll:true});window.addEventListener('resize',resize);window.visualViewport?.addEventListener('resize',resize);window.visualViewport?.addEventListener('scroll',resize)})
onBeforeUnmount(()=>{active=false;window.removeEventListener('resize',resize);window.visualViewport?.removeEventListener('resize',resize);window.visualViewport?.removeEventListener('scroll',resize);if(opener?.isConnected&&root.value?.contains(document.activeElement))opener.focus({preventScroll:true})})
</script>
<template>
  <section ref="root" data-council-window class="council-chat-window" :class="{enlarged}" :style="{width:bounds.width+'px',height:bounds.height+'px',bottom:(viewport.bottom+(viewport.width<=600?8:16))+'px',right:(viewport.right+(viewport.width<=600?8:16))+'px'}" role="dialog" :aria-labelledby="titleId" tabindex="-1" @keydown="keydown">
    <header class="window-head"><div class="window-title"><h2 :id="titleId">{{props.title}}</h2><div class="window-meta"><slot name="meta" /></div></div><div class="window-actions"><button type="button" class="btn btn-ghost" :aria-label="enlarged?'还原聊天小窗':'展开聊天大窗'" :aria-pressed="enlarged" @click="toggle">{{enlarged?'还原':'展开'}}</button><button type="button" class="btn btn-ghost" aria-label="关闭聊天小窗（讨论继续，草稿保留）" @click="emit('close')">关闭</button></div></header>
    <div class="window-risk" role="note"><slot name="risk" /></div>
    <div class="window-content">
      <div class="window-main" :inert="details" :aria-hidden="details || undefined"><slot /></div>
      <section v-if="details" class="window-details" aria-label="讨论详情面板"><header><b>结论、状态与证据</b><button class="btn btn-ghost" type="button" autofocus @click="closeDetails">返回聊天</button></header><div class="window-details-scroll"><slot name="details" /></div></section>
    </div>
    <footer class="window-tools"><button data-details-toggle class="btn btn-ghost" type="button" :disabled="props.detailsReady===false" :aria-expanded="details" @click="details ? closeDetails() : openDetails()">结论 / 证据 / 来源 / 状态详情</button><span>关闭不停止讨论</span></footer>
  </section>
</template>
<style scoped>
.council-chat-window{position:fixed;right:16px;bottom:16px;z-index:1200;display:flex;flex-direction:column;box-sizing:border-box;max-width:calc(100vw - 16px);max-height:calc(100dvh - 16px);background:var(--bg,#fff);color:var(--text,#202b29);border:1px solid #85988d66;border-radius:12px;box-shadow:0 12px 48px #0004;overflow:hidden;outline:none}
.window-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid #85988d44;flex-shrink:0}.window-title{min-width:0}.window-title h2{font-size:15px;margin:0;overflow-wrap:anywhere}.window-meta{font-size:11px;opacity:.8;margin-top:4px}.window-actions{display:flex;gap:4px;flex-shrink:0}.window-actions button{padding:5px 7px;font-size:12px}.window-risk{font-size:11px;line-height:1.5;padding:6px 12px;background:#b98b2314;max-height:64px;overflow:auto;flex-shrink:0;overflow-wrap:anywhere}
.window-content{flex:1;min-height:0;position:relative;display:flex}.window-main{flex:1;min-height:0;min-width:0;display:flex}.window-details{position:absolute;inset:0;z-index:2;background:var(--bg,#fff);display:flex;flex-direction:column;min-height:0}.window-details>header{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:8px 12px;border-bottom:1px solid #85988d44}.window-details-scroll{flex:1;min-height:0;overflow:auto;padding:12px;overscroll-behavior:contain}.window-tools{display:flex;justify-content:space-between;align-items:center;gap:6px;flex-shrink:0;padding:5px 10px;border-top:1px solid #85988d44}.window-tools button{font-size:11px;padding:4px}.window-tools span{font-size:10px;opacity:.7}
@media(max-width:600px){.council-chat-window{right:8px;bottom:8px;border-radius:9px}.window-head{padding:8px}.window-risk{padding:5px 8px}.window-title h2{font-size:14px}.window-tools span{display:none}}
</style>
