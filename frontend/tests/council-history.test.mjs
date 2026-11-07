import test from 'node:test'
import assert from 'node:assert/strict'
import { useCouncilHistory, createCouncilPoll } from '../src/composables/useCouncilHistory.ts'
const row = (id, content = 'old', status = 'done') => ({ id, created_at: '2026-11-07T13:00:00.000001+00:00', status, transcript: [{ content }], statements: [] })
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b}); return { promise, resolve, reject } }
const flush = () => new Promise(resolve => setTimeout(resolve, 0))
function setup(overrides = {}) {
  const api = { councilHistory: async () => ({items:[row(1), row(2)]}), councilGet: async id => row(id, 'fresh'), councilDelete: async () => ({deleted:true}), ...overrides }
  return useCouncilHistory(api)
}
test('opening completed history fetches detail immediately, switching cannot select a late old response', async () => {
  const a=deferred(), b=deferred(); const h=setup({councilGet:id=>id===1?a.promise:b.promise})
  await h.loadSessions(); const p=h.open(row(1)); const q=h.open(row(2));
  b.resolve(row(2,'new B'));await q;a.resolve(row(1,'new A'));await p
  assert.equal(h.expanded.value,2);assert.equal(h.sessions.value.find(r=>r.id===2).transcript[0].content,'new B')
})
test('new mutation cannot be overwritten by an older detail or list response', async () => {
  const d=deferred(), l=deferred();const h=setup({councilGet:()=>d.promise,councilHistory:()=>l.promise})
  const p=h.loadSessions();const q=h.loadSession(1);h.replaceSession(row(1,'user opinion / model / chair reply'))
  d.resolve(row(1,'stale detail'));l.resolve({items:[row(1,'stale list')]});await Promise.all([p,q])
  assert.equal(h.sessions.value[0].transcript[0].content,'user opinion / model / chair reply')
})
test('latest same-session request wins and old finally does not clear new loading', async () => {
  const a=deferred(),b=deferred();let n=0;const h=setup({councilGet:()=>++n===1?a.promise:b.promise})
  const p=h.loadSession(1),q=h.loadSession(1);a.resolve(row(1,'old'));await p
  assert.equal(h.detailLoading.value[1],true);b.resolve(row(1,'new'));await q
  assert.equal(h.detailLoading.value[1],false);assert.equal(h.sessions.value[0].transcript[0].content,'new')
})
test('cancel deletion performs no request; double click only sends once; deletion clears selection and late replies', async () => {
  const d=deferred(),g=deferred();let calls=0;const h=setup({councilDelete:()=>{calls++;return d.promise},councilGet:()=>g.promise})
  await h.loadSessions();h.expanded.value=1
  assert.equal(await h.deleteSession(1,()=>false),false);assert.equal(calls,0)
  const late=h.loadSession(1),p=h.deleteSession(1,()=>true),q=h.deleteSession(1,()=>true)
  d.resolve({deleted:true});await Promise.all([p,q]);g.resolve(row(1,'late'));await late
  h.replaceSession(row(1,'late mutation'));await h.loadSessions()
  assert.equal(calls,1);assert.equal(h.expanded.value,null);assert.equal(h.sessions.value.some(r=>r.id===1),false)
})
test('failed delete preserves details and reason; detail errors can retry', async () => {
  let fail=true;const h=setup({councilDelete:async()=>{throw {detail:'running conflict'}},councilGet:async id=>{if(fail)throw Error('network');return row(id,'recovered')}})
  await h.loadSessions();h.expanded.value=1;await h.deleteSession(1,()=>true)
  assert.equal(h.sessions.value.length,2);assert.equal(h.detailError.value[1],'running conflict')
  await h.loadSession(1);assert.equal(h.detailError.value[1],'network');fail=false;await h.loadSession(1)
  assert.equal(h.detailError.value[1],'');assert.equal(h.sessions.value[0].transcript[0].content,'recovered')
})
test('unmount suppresses late history/detail state changes', async () => {
  const a=deferred(),b=deferred();const h=setup({councilHistory:()=>a.promise,councilGet:()=>b.promise})
  const p=h.loadSessions(),q=h.loadSession(1);h.dispose();a.resolve({items:[row(1)]});b.resolve(row(1));await Promise.all([p,q]);assert.equal(h.sessions.value.length,0)
})
test('polling runs immediately, no overlapping or duplicate starts; stop suppresses in-flight result', async () => {
  const d=deferred();let calls=0,applied=0;const poll=createCouncilPoll(async current=>{calls++;await d.promise;if(current())applied++;return true},1)
  poll.start();poll.start();await flush();assert.equal(calls,1);poll.stop();d.resolve();await flush();await flush()
  assert.equal(calls,1);assert.equal(applied,0)
})
test('serial polling continues on completion and terminates cleanly', async () => {
  let calls=0;const poll=createCouncilPoll(async()=>++calls<2,1);poll.start()
  for(let i=0;i<10&&calls<2;i++)await flush()
  assert.equal(calls,2);poll.stop()
})

test('explicit new-run response can reuse SQLite highest deleted id without reviving old requests', async () => {
  const h=setup();await h.loadSessions();await h.deleteSession(2,()=>true)
  h.acceptNewSession(2);await h.loadSession(2);h.expanded.value=2
  assert.equal(h.sessions.value.find(r=>r.id===2).transcript[0].content,'fresh')
})

test('history discovers a different reused id created in another tab; old deleted identity stays hidden', async () => {
  let fresh=false;const h=setup({councilHistory:async()=>({items:[{...row(1),created_at:fresh?'2026-11-07T13:00:00.000002+00:00':row(1).created_at}]})})
  await h.loadSessions();await h.deleteSession(1,()=>true);await h.loadSessions();assert.equal(h.sessions.value.length,0)
  fresh=true;await h.loadSessions();assert.equal(h.sessions.value.length,1)
})

test('switch cancels old GET, failure never marks a list snapshot fresh, retry recovers', async () => {
  const signals=[];let fail=true
  const h=setup({councilGet:async(id,signal)=>{signals.push(signal);if(id===1)return new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError'))));if(fail)throw Error('network');return row(id,'fresh B')}})
  await h.loadSessions();const old=h.open(row(1));await h.open(row(2));await old
  assert.equal(signals[0].aborted,true);assert.equal(h.expanded.value,2);assert.equal(h.detailReady.value[2],undefined);assert.equal(h.detailError.value[1],'')
  fail=false;await h.loadSession(2);assert.equal(h.detailReady.value[2],row(2).created_at);assert.equal(h.opening.value,null)
})
test('dispose aborts actual GET/list signals and suppresses late responses', async () => {
  const signals=[]
  const pending=(_arg,signal)=>{signals.push(signal);return new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError'))))}
  const h=setup({councilGet:pending,councilHistory:pending});const p=h.loadSessions(),q=h.loadSession(1);h.dispose();await Promise.all([p,q]);assert.equal(signals.every(s=>s.aborted),true);assert.equal(h.sessions.value.length,0)
})

test('close only closes UI/cancels stale detail; running record survives and reopening fetches latest',async()=>{
 const first=deferred();let calls=0,signal;const h=setup({councilGet:async(id,s)=>{signal=s;if(++calls===1)return first.promise;return row(id,'new reply after reopening','running')}})
 await h.loadSessions();const pending=h.open(row(1,'old','running'));h.close();assert.equal(signal.aborted,true);assert.equal(h.expanded.value,null);assert.equal(h.opening.value,null);assert.equal(h.sessions.value.length,2)
 const reopen=h.open(row(1,'old','running'));await reopen;first.resolve(row(1,'stale'));await pending
 assert.equal(calls,2);assert.equal(h.expanded.value,1);assert.equal(h.sessions.value[0].transcript[0].content,'new reply after reopening');assert.equal(h.sessions.value[0].status,'running')
})
