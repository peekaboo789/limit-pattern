import test from 'node:test'
import assert from 'node:assert/strict'
import { councilWindowBounds, useCouncilWindow, compactWindowMessages, councilWindowInsets } from '../src/composables/councilWindow.ts'
import { readFileSync } from 'node:fs'
test('compact desktop is 460x650, expanded stays bounded, mobile is nearly fullscreen',()=>{
 assert.deepEqual(councilWindowBounds(1440,900),{width:460,height:650})
 assert.deepEqual(councilWindowBounds(1440,900,true),{width:1000,height:840})
 assert.deepEqual(councilWindowBounds(390,844),{width:374,height:828})
 assert.deepEqual(councilWindowBounds(390,844,true),{width:374,height:828})
 for(const [w,h] of [[320,200],[900,420],[601,300],[1,1]])for(const expanded of [false,true]){const b=councilWindowBounds(w,h,expanded);assert.ok(b.width>0&&b.width<=w);assert.ok(b.height>0&&b.height<=h)}
})
test('size is local state, preserves detail state; Escape closes panel first and respects IME',()=>{
 const w=useCouncilWindow();w.details.value=true;w.toggleSize();assert.equal(w.enlarged.value,true);assert.equal(w.details.value,true)
 assert.equal(w.escape(true),'none');assert.equal(w.details.value,true);assert.equal(w.escape(),'details');assert.equal(w.details.value,false);assert.equal(w.enlarged.value,true)
 w.toggleSize();assert.equal(w.enlarged.value,false);assert.equal(w.escape(),'close')
})
test('layout contract: fixed bounded shell, flex message area and separate non-overlay composer',()=>{
 const frame=readFileSync(new URL('../src/components/CouncilChatWindow.vue',import.meta.url),'utf8'),view=readFileSync(new URL('../src/views/CouncilView.vue',import.meta.url),'utf8')
 assert.ok(frame.includes('position:fixed'));assert.ok(frame.includes('100dvh'));assert.ok(frame.includes('window.visualViewport'));assert.ok(frame.includes('window.removeEventListener'));assert.ok(frame.includes('event.target instanceof HTMLSelectElement'))
 assert.ok(view.includes('<Teleport to="body">'));assert.ok(view.includes('expanded === item.id'));assert.ok(view.includes('@close="closeChat"'));assert.ok(view.includes('rememberChat(); history.close()'))
 assert.ok(view.includes('.chat-viewport{flex:1;height:auto;min-height:0'));assert.ok(view.includes('.chat-composer{flex-shrink:0'));assert.ok(frame.includes(':inert="details"'));assert.equal(frame.includes('api.'),false);assert.ok(view.includes('composing.value[key]=false;compositionEnded.set(key,Date.now())'));assert.ok(view.includes('editor instanceof HTMLTextAreaElement'));assert.ok(view.includes('new ResizeObserver'));assert.ok(view.includes('viewportObserver?.disconnect()'))
})

test('small window folds old stages without hiding any speech or session failure',()=>{
 const messages=[{key:'s:stage:1',kind:'system'},{key:'s:speech:1',kind:'user'},{key:'s:stage:2',kind:'system'},{key:'s:speech:2',kind:'model'},{key:'s:error',kind:'system'}]
 assert.deepEqual(compactWindowMessages(messages).map(m=>m.key),['s:speech:1','s:stage:2','s:speech:2','s:error'])
 assert.deepEqual(compactWindowMessages([]),[])
})

test('keyboard/panned visual viewport moves bottom and right anchors into visible area',()=>{
 assert.deepEqual(councilWindowInsets(390,844,{width:390,height:420,offsetTop:0,offsetLeft:0}),{right:0,bottom:424})
 assert.deepEqual(councilWindowInsets(390,844,{width:300,height:420,offsetTop:100,offsetLeft:40}),{right:50,bottom:324})
 assert.deepEqual(councilWindowInsets(390,844,null),{right:0,bottom:0})
})
