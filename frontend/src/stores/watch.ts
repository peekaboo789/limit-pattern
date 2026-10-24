import { api, ApiError } from '@/api/client'
import type { IntradayWatchState, WatchAlert } from '@/api/types'
import { reactive } from 'vue'

/**
 * 盘中「涨幅报警榜」的读数与放声，只留这一份。
 *
 * 后端已经按 `poll_seconds` 的节奏在后台线程里采好了（`GET /api/realtime/intraday-watch`），
 * 这里只是读那一份并把「该敲一下用户」的那件事做到浏览器里。四件事说在前头：
 *
 * * **节奏跟着后端那个常量走**：前端不另定一秒几次的数，否则两边读数会互相抢带宽；
 *   阈值、档位标识（`alert_tier`/`pin_tier`）同理，一律从回包里读——
 *   在前端抄一份 `'spike_6pct'` 就是第四处可以改动的地方（库里的账、判据、常量、页面）。
 * * **单飞闸门 + 读不到维持上一份**：网络抖一下不该把已经看见的榜抹掉，
 *   只有「一次都没读到过」才配得上红字。
 * * **放声的账在模块级**：判据是「这一条在本次浏览里敲过没有」，不是「它今天报过没有」——
 *   后者在库里。换页再回来不该让那十几条旧报警同时响一遍，所以这份账不跟着组件卸载而清空。
 *   第一次读到的那些只摆进浮层、不放声。
 * * **点「知道了」当场生效**：浮层当场收起用的是本地这一次判断（后端已经写了库），
 *   等下一次轮询才收起的话，那颗按钮看起来就是坏的。
 */

const MIN_TICK_MS = 1000
const MAX_TICK_MS = 10000

export const watch = reactive({
  state: null as IntradayWatchState | null,
  /** 一次都没读到过——只有这时候才该红。 */
  error: '',
  /** 读到过，这一拍失败了：屏幕上还是上一份，所以只能是提示。 */
  note: '',
  ackBusy: false,
  /** 浏览器要先有了一次点击才允许网页出声；没解锁时浮层上这句话必须照实说。 */
  soundReady: false,
})

let refs = 0
let timer = 0
let inFlight = false

function detail(reason: unknown, fallback: string): string {
  return reason instanceof ApiError ? reason.detail : fallback
}

function tickMs(): number {
  const seconds = watch.state?.poll_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return 3000
  return Math.min(MAX_TICK_MS, Math.max(MIN_TICK_MS, Math.round(seconds * 1000)))
}

// --- 声音：浏览器不允许页面自己开始放声，得先有这一次点击 ---------------------

let audio: AudioContext | null = null

function unlockSound(): void {
  try {
    audio ??= new AudioContext()
    if (audio.state === 'suspended') void audio.resume()
    watch.soundReady = audio.state === 'running'
  } catch {
    // 没有 WebAudio（或已被系统禁用）：浮层上那句话必须照实说，不能假装响过
    watch.soundReady = false
  }
}

/**
 * 一短两短的提示音。置顶档比弹窗档多响一层、频率更高——
 * 用户听到就该分得出「这是叫你去看一眼」还是「这是要把窗口摞到最前面」。
 */
function ring(isPin: boolean): void {
  // 先在局部拿住那一个实例：`audio` 是会被别处置空的模块级变量，
  // 回调里直接读它，类型上就永远得猜「这一行它还在不在」。
  const ctx = audio
  if (!ctx || ctx.state !== 'running') return
  const start = ctx.currentTime
  const plan = isPin ? [880, 1176, 1568] : [660, 880]
  plan.forEach((freq, index) => {
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    const at = start + index * 0.18
    gain.gain.setValueAtTime(0.0001, at)
    gain.gain.exponentialRampToValueAtTime(isPin ? 0.28 : 0.18, at + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.16)
    osc.connect(gain).connect(ctx.destination)
    osc.start(at)
    osc.stop(at + 0.18)
  })
}

// --- 标题闪烁：这一页不在前台时，标签页上得看得见「有事没确认」 ----------------

let flashTimer = 0
let baseTitle = ''
let flashOn = false
let flashLabel = 0

function startFlash(count: number): void {
  flashLabel = count
  if (flashTimer) return
  baseTitle = document.title
  flashTimer = window.setInterval(() => {
    flashOn = !flashOn
    document.title = flashOn ? `⚠ 涨幅报警 ${flashLabel} 条待确认` : baseTitle
  }, 1200)
  flashOn = false
  document.title = baseTitle
}

function stopFlash(): void {
  if (flashTimer) {
    window.clearInterval(flashTimer)
    flashTimer = 0
  }
  if (baseTitle && document.title !== baseTitle) document.title = baseTitle
  flashOn = false
}

// --- 放声的账 -----------------------------------------------------------------

const announced = new Set<string>()
/** 第一次读到的那些不放声，只摆进浮层 */
let firstRead = true

function keyOf(item: WatchAlert): string {
  return `${item.trade_date}|${item.stock_code}|${item.tier}`
}

function announce(body: IntradayWatchState): void {
  const unacked = body.unacked ?? []
  const pin = body.pin_tier
  if (firstRead) {
    unacked.forEach((item) => announced.add(keyOf(item)))
    firstRead = false
  } else {
    for (const item of unacked) {
      const key = keyOf(item)
      if (announced.has(key)) continue
      announced.add(key)
      ring(item.tier === pin)
    }
  }
  if (unacked.length) startFlash(unacked.length)
  else stopFlash()
}

// --- 轮询：等上一条回来再发下一条 ---------------------------------------------

async function poll(): Promise<void> {
  if (inFlight) return
  inFlight = true
  try {
    const body = await api.intradayWatch()
    watch.state = body
    watch.error = ''
    watch.note = ''
    announce(body)
  } catch (reason) {
    const message = detail(reason, '涨幅报警榜读取失败')
    if (watch.state) watch.note = `${message}，屏幕上还是上一份`
    else watch.error = message
  } finally {
    inFlight = false
    if (!refs) {
      timer = 0
      return
    }
    timer = window.setTimeout(() => void poll(), tickMs())
  }
}

// --- 「知道了」 ---------------------------------------------------------------

/**
 * 确认一条（或全部）。
 *
 * 浮层当场收起用的是**本地这一次判断**：从 `unacked` 里摘掉刚确认的那几条。
 * 后端已经写了库、也把那份账就地换进了它的状态里，所以紧接着这一次重读拿到的是同一份事实
 * （含库里的确认时刻）。不重读的话，榜上那几行的「已确认」标记会停在旧值上。
 */
export async function ackWatch(stockCode?: string, tier?: string): Promise<void> {
  if (watch.ackBusy) return
  watch.ackBusy = true
  let failure = ''
  try {
    const body = await api.ackIntradayWatch(stockCode, tier)
    if (body.ok === false) failure = body.error || '后端没说原因'
    else dropLocally(stockCode, tier)
  } catch (reason) {
    failure = detail(reason, '「知道了」提交失败')
  } finally {
    watch.ackBusy = false
  }
  if (failure) {
    watch.note = `这次「知道了」没写进库：${failure}`
    return
  }
  await poll()
}

function dropLocally(stockCode?: string, tier?: string): void {
  const body = watch.state
  if (!body) return
  const matches = (item: WatchAlert) =>
    (!stockCode || item.stock_code === stockCode) && (!tier || item.tier === tier)
  const left = body.unacked.filter((item) => !matches(item))
  const pins = left.filter((item) => item.tier === body.pin_tier).map((item) => item.stock_code)
  watch.state = {
    ...body,
    unacked: left,
    pending_pin_codes: [...new Set(pins)].sort(),
    pinning: pins.length > 0,
  }
  if (!left.length) stopFlash()
}

// --- 挂载与节奏 ---------------------------------------------------------------

export function retainWatch(): void {
  refs += 1
  if (refs > 1) return
  window.addEventListener('pointerdown', unlockSound)
  window.addEventListener('keydown', unlockSound)
  if (timer) window.clearTimeout(timer)
  timer = window.setTimeout(() => void poll(), 0)
}

export function releaseWatch(): void {
  refs = Math.max(0, refs - 1)
  if (refs > 0) return
  window.removeEventListener('pointerdown', unlockSound)
  window.removeEventListener('keydown', unlockSound)
  stopFlash()
  if (timer) {
    window.clearTimeout(timer)
    timer = 0
  }
}

/** 回到前台或手动刷新那一拍立刻补一次：读数不该等下一次定时才追上。 */
export function refreshWatchNow(): void {
  if (!refs) return
  if (timer) window.clearTimeout(timer)
  timer = window.setTimeout(() => void poll(), 0)
}
