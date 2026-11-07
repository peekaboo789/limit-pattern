import type { CouncilSession, CouncilTranscriptEntry } from '../api/types'

export interface ChatMessage { key: string; kind: string; content: string; name: string; time: string | null; error?: string; model?: string; elapsed?: number | null }
export const councilIdentity = (row: Pick<CouncilSession, 'id' | 'created_at'>) => `${row.id}:${row.created_at || 'legacy'}`
const instant = (value?: string | null) => value ? Date.parse(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`) : NaN
export function publicContent(text: string) { return (text || '').replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '').trim() }
export function transcriptItems(row: CouncilSession): CouncilTranscriptEntry[] {
  return row.transcript?.length ? row.transcript : (row.statements || []).map((item, index) => ({
    seq: index + 1, kind: 'model', profile_id: null, speaker: item.speaker, model: item.model,
    round: null, content: item.content, error: item.error, status: item.error ? 'failed' : 'done',
    started_at: null, finished_at: null, elapsed_ms: null,
  }))
}
export function chatMessages(row: CouncilSession): ChatMessage[] {
  const prefix = councilIdentity(row)
  // Transcript seq is authoritative. Repeated snapshots/duplicate entries are NOT new messages.
  const entries = new Map<string, CouncilTranscriptEntry & {seq:number;stableKey:string}>()
  transcriptItems(row).forEach((item,index) => {
    const valid=typeof item.seq==='number'&&Number.isSafeInteger(item.seq)&&item.seq>0
    const stableKey=valid?String(item.seq):`legacy:${JSON.stringify([item.kind,item.model,item.started_at,item.content,item.error])}`
    entries.set(stableKey,{...item,seq:valid?item.seq!:index+1,stableKey})
  })
  const speech = [...entries.values()].sort((a,b) => a.seq-b.seq)
  const events = new Map<string, ChatMessage>()
  for (const event of row.progress?.events || []) {
    const key = `${prefix}:stage:${JSON.stringify([event.phase,event.stage,event.model,event.started_at])}`
    events.set(key, {key, kind:'system', name:'系统', content:`${event.stage || event.phase || '处理中'}${event.model ? ` · ${event.model}` : ''}${event.finished_at ? '' : ' · 正在处理'}`, time:event.started_at || null})
  }
  const orderedEvents = [...events.values()].sort((a,b) => (instant(a.time)||0)-(instant(b.time)||0))
  const result: ChatMessage[] = []
  const times=speech.map(item=>instant(item.started_at)), nextTimes=[...times]
  for(let i=nextTimes.length-2;i>=0;i--)if(!Number.isFinite(nextTimes[i]))nextTimes[i]=nextTimes[i+1]!
  let cursor = 0, previous = -Infinity
  speech.forEach((item,index) => {
    // Unknown legacy timestamps affect ordering only; never invent a displayed timestamp.
    const next = Number.isFinite(nextTimes[index])?nextTimes[index]:undefined
    const raw = times[index]!
    const time = Math.max(previous + 0.001, Number.isFinite(raw) ? raw : (next ?? (instant(row.created_at)||0)) - (speech.length-index)*0.001)
    previous = time
    while (cursor < orderedEvents.length && (instant(orderedEvents[cursor]!.time)||0) <= time) result.push(orderedEvents[cursor++]!)
    result.push({key:`${prefix}:speech:${item.stableKey}`,kind:item.kind,content:item.content || '',name:item.kind==='user'?'我':(item.speaker || item.model || '模型'),time:item.started_at || null,error:item.error,model:item.model,elapsed:item.elapsed_ms})
  })
  result.push(...orderedEvents.slice(cursor))
  if (row.error) result.push({key:`${prefix}:error`,kind:'system',name:'系统',content:`失败 / 超时：${row.error}`,time:row.finished_at})
  return result
}
export function nearBottom(height: number, top: number, client: number) { return height-top-client <= 90 }
export function shouldSendKey(event: Pick<KeyboardEvent,'key'|'shiftKey'|'altKey'|'isComposing'|'keyCode'>, composing: boolean, sinceComposition: number) {
  return event.key==='Enter' && !event.shiftKey && !event.altKey && !event.isComposing && !composing && event.keyCode!==229 && sinceComposition>100
}
export function clearSubmittedDraft(current: string, submitted: string) { return current === submitted ? '' : current }

export interface ScrollPosition { top: number; follow: boolean; unread: number; seen: Set<string> }
export function createChatScroll() {
  const positions = new Map<string, ScrollPosition>()
  const state = (id: string) => { if (!positions.has(id)) positions.set(id,{top:0,follow:true,unread:0,seen:new Set()}); return positions.get(id)! }
  return {
    state,
    scroll(id: string, height: number, top: number, client: number) { const s=state(id);s.top=top;s.follow=nearBottom(height,top,client);if(s.follow)s.unread=0 },
    messages(id: string, keys: string[]) { const s=state(id);const fresh=keys.filter(key=>!s.seen.has(key)); if(s.seen.size && !s.follow)s.unread+=fresh.length; keys.forEach(key=>s.seen.add(key));return s },
    bottom(id: string) {const s=state(id);s.follow=true;s.unread=0},
    remove(id: string) {positions.delete(id)},
  }
}
export type InlinePart = {text:string;href?:string}
export function inlineParts(text: string): InlinePart[] {
  const pattern = /\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]{1,2048})\)|(https?:\/\/[^\s<>]{1,2048})/g
  const parts: InlinePart[]=[];let end=0
  for(const match of text.matchAll(pattern)) {
    const offset=match.index!;if(offset>end)parts.push({text:text.slice(end,offset)})
    const href=match[2] || match[3] || '';let safe=false
    try {const url=new URL(href);safe=['https:','http:'].includes(url.protocol)&&!url.username&&!url.password}catch{/* plain text */}
    parts.push(safe?{text:match[1]||href,href}:{text:match[0]});end=offset+match[0].length
  }
  if(end<text.length)parts.push({text:text.slice(end)})
  return parts
}
export type MessageBlock = {kind:'text';text:string}|{kind:'code';text:string;language:string}|{kind:'table';head:string[];rows:string[][]}
export function messageBlocks(text: string): MessageBlock[] {
  const lines=text.split('\n'), result:MessageBlock[]=[];let plain:string[]=[]
  const flush=()=>{if(plain.length){result.push({kind:'text',text:plain.join('\n')});plain=[]}}
  const cells=(line:string)=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(cell=>cell.trim())
  for(let i=0;i<lines.length;i++) {
    const line=lines[i]!,fence=line.match(/^\s*```([\w+-]{0,24})\s*$/)
    if(fence){flush();const code:string[]=[];while(++i<lines.length&&!/^\s*```\s*$/.test(lines[i]!))code.push(lines[i]!);result.push({kind:'code',text:code.join('\n'),language:fence[1]||''});continue}
    const divider=lines[i+1]
    if(line.includes('|')&&divider&&cells(divider).length>1&&cells(divider).every(cell=>/^:?-{3,}:?$/.test(cell))){flush();const head=cells(line),rows:string[][]=[];i++;while(i+1<lines.length&&lines[i+1]!.includes('|')&&lines[i+1]!.trim()){const row=cells(lines[++i]!);rows.push(head.map((_,index)=>row[index]||''))}result.push({kind:'table',head,rows});continue}
    plain.push(line)
  }
  flush();return result
}

export function currentStage(row: CouncilSession, now=Date.now()) {
  const stage=row.progress?.stage || '准备中', model=row.progress?.current_model || ''
  const events=row.progress?.events || [],event=events[events.length-1]
  const start=instant(event?.started_at),end=event?.finished_at?instant(event.finished_at):now
  return {label:model&&!stage.includes(model)?`${stage} · ${model}`:stage,seconds:Number.isFinite(start)&&Number.isFinite(end)&&end>=start?Math.floor((end-start)/1000):null}
}

export function conversationStatus(row:CouncilSession) {
  const modelDone=transcriptItems(row).some(item=>item.kind!=='user'&&!!item.content&&!item.error)
  if(row.status==='done')return row.conclusion?'完成':modelDone?'待主持人总结':'待模型发言'
  if(row.status==='failed')return modelDone?'部分完成 · 有失败':'失败'
  return '进行中'
}
