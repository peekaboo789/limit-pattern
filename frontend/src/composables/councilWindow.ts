import { ref } from 'vue'
import type { ChatMessage } from './councilChat'

/** Local presentation only: never starts/stops a discussion or makes a request. */
export function useCouncilWindow() {
  const enlarged=ref(false), details=ref(false)
  return {enlarged,details,toggleSize(){enlarged.value=!enlarged.value},
    escape(composing=false){if(composing)return 'none';if(details.value){details.value=false;return 'details'}return 'close'},
  }
}
export function councilWindowBounds(width:number,height:number,enlarged=false) {
  const w=Number.isFinite(width)?Math.max(1,width):1280,h=Number.isFinite(height)?Math.max(1,height):800
  const compact=w<=600,margin=compact?8:16
  return {width:Math.min(compact?Math.max(1,w-margin*2):enlarged?1000:460,Math.max(1,w-margin*2)),
    height:Math.min(compact?Math.max(1,h-margin*2):enlarged?840:650,Math.max(1,h-margin*2))}
}

/** Keep the latest status and every failure; stage history remains in the details panel. */
export function compactWindowMessages(messages:ChatMessage[]) {
 const statuses=messages.filter(item=>item.kind==='system'&&!item.key.endsWith(':error'))
 const last=statuses[statuses.length-1]?.key
 return messages.filter(item=>item.kind!=='system'||item.key.endsWith(':error')||item.key===last)
}

export function councilWindowInsets(width:number,height:number,visual?:{width:number;height:number;offsetLeft:number;offsetTop:number}|null) {
 if(!visual)return {right:0,bottom:0}
 return {right:Math.max(0,width-visual.width-visual.offsetLeft),bottom:Math.max(0,height-visual.height-visual.offsetTop)}
}
