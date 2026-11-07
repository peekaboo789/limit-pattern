import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
let server, component
before(async()=>{
 const root=fileURLToPath(new URL('..',import.meta.url))
 server=await createServer({root,configFile:false,cacheDir:root+'/node_modules/.vite-council-ssr',optimizeDeps:{noDiscovery:true,include:[],entries:[]},plugins:[vue()],resolve:{alias:{'@':root+'/src'}},server:{middlewareMode:true,hmr:false,watch:null}})
 component=(await server.ssrLoadModule('/src/components/CouncilMessageContent.vue')).default
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
