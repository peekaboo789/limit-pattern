import test from 'node:test'
import assert from 'node:assert/strict'
import { chatMessages, councilIdentity, createChatScroll, nearBottom, shouldSendKey, clearSubmittedDraft, inlineParts, messageBlocks, publicContent, currentStage, conversationStatus } from '../src/composables/councilChat.ts'
const entry=(seq,content,extra={})=>({seq,kind:'model',speaker:'甲',model:'model-a',content,error:'',started_at:`2026-11-07T13:00:0${seq}Z`,...extra})
const row=(transcript,events=[])=>({id:1,created_at:'2026-11-07T12:59:00Z',finished_at:null,error:'',transcript,statements:[],progress:{events}})
test('transcript seq authoritative, duplicate snapshot entries collapse with stable keys',()=>{
 const a=row([entry(2,'second'),entry(1,'first'),entry(2,'second')]);const b=row([entry(1,'first'),entry(2,'second')])
 assert.deepEqual(chatMessages(a).map(m=>m.content),['first','second']);assert.deepEqual(chatMessages(a).map(m=>m.key),chatMessages(b).map(m=>m.key))
})
test('repeated status events dedupe, keys survive event truncation and finishing',()=>{
 const e={phase:'speaking',stage:'模型发言',model:'甲',started_at:'2026-11-07T13:00:00Z'}
 const a=chatMessages(row([entry(1,'a')],[e,e]));assert.equal(a.filter(m=>m.kind==='system').length,1)
 const finished=chatMessages(row([entry(1,'a')],[{...e,finished_at:'2026-11-07T13:00:02Z'}]));assert.equal(a[0].key,finished[0].key)
})
test('clock drift cannot reorder persisted speech seq, legacy missing seq remains visible',()=>{
 const m=chatMessages(row([entry(1,'first',{started_at:'2026-11-07T13:00:10Z'}),entry(2,'second',{started_at:'2026-11-07T13:00:00Z'})]));assert.deepEqual(m.map(x=>x.content),['first','second'])
 assert.equal(chatMessages(row([entry(undefined,'legacy')])).length,1)
})
test('legacy unknown timestamps stay ahead of newer persisted dialogue without invented display time',()=>{
 const m=chatMessages(row([entry(1,'legacy',{started_at:null}),entry(2,'new')]));assert.equal(m[0].content,'legacy');assert.equal(m[0].time,null)
})
test('scroll tracks only near-bottom, unread counts only new keys, repeated polls do not multiply',()=>{
 assert.equal(nearBottom(1000,700,210),true);assert.equal(nearBottom(1000,699,210),false)
 const s=createChatScroll();s.messages('A',['1']);s.scroll('A',1000,100,200)
 assert.equal(s.messages('A',['1','2']).unread,1);assert.equal(s.messages('A',['1','2']).unread,1)
 assert.equal(s.state('A').top,100);s.bottom('A');assert.equal(s.state('A').unread,0)
})
test('history scroll and drafts use distinct session/incarnation identities',()=>{
 const a=councilIdentity({id:1,created_at:'a'}),b=councilIdentity({id:1,created_at:'b'});assert.notEqual(a,b)
 const s=createChatScroll();s.scroll(a,1000,100,200);s.scroll(b,1000,800,200);assert.equal(s.state(a).top,100);assert.equal(s.state(a).follow,false);assert.equal(s.state(b).follow,true)
 const drafts={[a]:'草稿甲',[b]:'草稿乙'};assert.equal(drafts[a],'草稿甲');assert.equal(drafts[b],'草稿乙')
})
test('Chinese IME, 229 keycode, composition-end guard and Shift+Enter never send',()=>{
 const e={key:'Enter',shiftKey:false,altKey:false,isComposing:false,keyCode:13}
 assert.equal(shouldSendKey(e,false,1000),true)
 for(const [event,composing,elapsed] of [[{...e,isComposing:true},false,1000],[e,true,1000],[{...e,keyCode:229},false,1000],[e,false,50],[{...e,shiftKey:true},false,1000],[{...e,altKey:true},false,1000]])assert.equal(shouldSendKey(event,composing,elapsed),false)
})
test('successful persisted send clears only submitted draft, not text typed during save',()=>{
 assert.equal(clearSubmittedDraft('原文','原文'),'');assert.equal(clearSubmittedDraft('正在写下一条','原文'),'正在写下一条')
})
test('safe rich text handles table/code/links without interpreting HTML or unsafe URL protocols',()=>{
 const blocks=messageBlocks('说明\n|字段|数值|\n|---|---|\n|EPS|0.1|\n```js\n<script>alert(1)</script>\n```\n[来源](https://example.com/report)')
 assert.equal(blocks[1].kind,'table');assert.deepEqual(blocks[1].rows,[['EPS','0.1']]);assert.equal(blocks[2].kind,'code');assert.ok(blocks[2].text.includes('<script>'))
 const parts=inlineParts('<img onerror=alert(1)> [危险](javascript:alert(1)) [来源](https://example.com) https://user:pass@example.com')
 assert.equal(parts.filter(p=>p.href).length,1);assert.equal(parts.find(p=>p.href).href,'https://example.com');assert.ok(parts.map(p=>p.text).join('').includes('<img'))
})
test('raw private think blocks excluded from model output, non-hidden analysis kept',()=>{
 assert.equal(publicContent('<think>private</think>公开依据'),'公开依据');assert.equal(publicContent('<think>unfinished'),'')
})

test('current model stage shows real event timing, unknown or future clock is not invented',()=>{
 const r=row([],[{phase:'speaking',stage:'发言中',model:'甲',started_at:'2026-11-07T13:00:00Z'}]);r.progress.stage='发言中';r.progress.current_model='甲'
 assert.deepEqual(currentStage(r,Date.parse('2026-11-07T13:00:05Z')),{label:'发言中 · 甲',seconds:5})
 assert.equal(currentStage(row([])).seconds,null);assert.equal(currentStage(r,0).seconds,null)
})

test('empty/failed/partial labels distinguish model completion from user-only messages',()=>{
 assert.equal(conversationStatus({...row([]),status:'done',conclusion:null}),'待模型发言')
 assert.equal(conversationStatus({...row([entry(1,'意见',{kind:'user'})]),status:'failed'}),'失败')
 assert.equal(conversationStatus({...row([entry(1,'模型公开结果')]),status:'failed'}),'部分完成 · 有失败')
 assert.equal(conversationStatus({...row([entry(1,'模型公开结果')]),status:'done',conclusion:null}),'待主持人总结')
})
