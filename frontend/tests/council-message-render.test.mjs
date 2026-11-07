import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
let server, component, windowComponent
before(async()=>{
 const root=fileURLToPath(new URL('..',import.meta.url))
 server=await createServer({root,configFile:false,cacheDir:root+'/node_modules/.vite-council-ssr',optimizeDeps:{noDiscovery:true,include:[],entries:[]},plugins:[vue()],resolve:{alias:{'@':root+'/src'}},server:{middlewareMode:true,hmr:false,watch:null}})
 component=(await server.ssrLoadModule('/src/components/CouncilMessageContent.vue')).default
 windowComponent=(await server.ssrLoadModule('/src/components/CouncilChatWindow.vue')).default
})
after(async()=>{await server?.close()})
test('actual Vue message component escapes HTML while rendering safe links, code and tables',async()=>{
 const text='<img src=x onerror=alert(1)>\n|列|内容|\n|---|---|\n|x|<svg onload=alert(1)>|\n```html\n<script>alert(1)</script>\n```\n[来源](https://example.com/report)'
 const html=await renderToString(createSSRApp(component,{text}))
 assert.ok(html.includes('<table'));assert.ok(html.includes('<pre'));assert.ok(html.includes('&lt;script&gt;'));assert.ok(html.includes('&lt;img'))
 assert.equal(html.includes('<img'),false);assert.equal(html.includes('<svg'),false);assert.equal(html.includes('<script>'),false)
 assert.ok(html.includes('href="https://example.com/report"'));assert.ok(html.includes('noopener noreferrer'))
})
test('actual component never converts unsafe protocols or credential links into anchors',async()=>{
 const html=await renderToString(createSSRApp(component,{text:'[恶意](javascript:alert(1)) [数据](data:text/html,test) https://user:pass@example.com'}))
 assert.equal(html.includes('<a '),false)
})

test('actual floating window renders bounded dialog, persistent risk and separate chat/details slots',async()=>{
 const html=await renderToString(createSSRApp({render:()=>h(windowComponent,{title:'股票 <script>'},{default:()=>h('div',{'data-test':'chat'},'消息'),risk:()=>h('b','证据不足，不可估计'),meta:()=>h('span','3位模型'),details:()=>h('div',{'data-test':'evidence'},'完整证据')})}))
 assert.ok(html.includes('role="dialog"'));assert.ok(html.includes('width:460px'));assert.ok(html.includes('height:650px'));assert.ok(html.includes('data-council-window'))
 assert.ok(html.includes('证据不足，不可估计'));assert.ok(html.includes('data-test="chat"'));assert.equal(html.includes('data-test="evidence"'),false)
 assert.ok(html.includes('关闭不停止讨论'));assert.ok(html.includes('草稿保留'));assert.ok(html.includes('&lt;script&gt;'));assert.equal(html.includes('<script>'),false)
 assert.ok(html.includes('aria-pressed="false"'));assert.equal(html.includes('inert="false"'),false)
})

test('floating details entry is disabled while first latest detail is unconfirmed',async()=>{
 const html=await renderToString(createSSRApp(windowComponent,{title:'加载中',detailsReady:false}))
 assert.match(html,/data-details-toggle[^>]*disabled/);assert.equal(html.includes('window-details-scroll'),false)
})
