import type {
  KBar,
  KlineResponse,
  PoolItem,
  ScanItem,
  ScanResponse,
  SimilarityMatch,
  SimilarityResponse,
  SummaryDocument,
} from '@/api/types'

export const DATA_DATE = '2026-10-22'
const HISTORY = 140

const CLOSED = new Set(['2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07'])

interface StockInfo {
  code: string
  name: string
  sector: string
  base: number
}

interface LimitEvent {
  code: string
  name: string
  sector: string
  board: 'SH' | 'SZ'
  limit_date: string
  consecutive_boards: number
  limit_type: string
  first_limit_time: string | null
  turnover_rate: number
}

type Streak = [string, string, number, string, string | null, number]

const STOCK_ROWS: Array<[string, string, string, number]> = [
  ['300750', '宁德时代', '电力设备', 268],
  ['002475', '立讯精密', '电子', 42],
  ['300274', '阳光电源', '电力设备', 96],
  ['002594', '比亚迪', '汽车', 108],
  ['300059', '东方财富', '非银金融', 26],
  ['600089', '特变电工', '电力设备', 18],
  ['601012', '隆基绿能', '电力设备', 16.5],
  ['002371', '北方华创', '电子', 320],
  ['688981', '中芯国际', '电子', 92],
  ['000725', '京东方A', '电子', 4.2],
  ['300502', '新易盛', '通信', 185],
  ['300394', '天孚通信', '通信', 92],
  ['002241', '歌尔股份', '电子', 28],
  ['603799', '华友钴业', '有色金属', 42],
  ['002056', '横店东磁', '电力设备', 16],
  ['300661', '圣邦股份', '电子', 78],
  ['601669', '中国电建', '建筑装饰', 8.2],
  ['688012', '中微公司', '电子', 210],
  ['300308', '中际旭创', '通信', 155],
  ['300124', '汇川技术', '机械设备', 72],
  ['688041', '海光信息', '电子', 140],
  ['002230', '科大讯飞', '计算机', 52],
  ['600276', '恒瑞医药', '医药生物', 48],
  ['000858', '五粮液', '食品饮料', 128],
  ['603259', '药明康德', '医药生物', 68],
  ['601138', '工业富联', '电子', 24],
  ['002415', '海康威视', '计算机', 31],
  ['000063', '中兴通讯', '通信', 34],
  ['002074', '国轩高科', '电力设备', 28],
  ['600111', '北方稀土', '有色金属', 32],
  ['300418', '昆仑万维', '传媒', 46],
  ['002466', '天齐锂业', '有色金属', 42],
  ['600519', '贵州茅台', '食品饮料', 1510],
  ['000001', '平安银行', '银行', 11.6],
  ['601318', '中国平安', '非银金融', 48],
  ['002352', '顺丰控股', '交通运输', 39],
  ['300014', '亿纬锂能', '电力设备', 62],
  ['600030', '中信证券', '非银金融', 26],
  ['000333', '美的集团', '家用电器', 72],
  ['601899', '紫金矿业', '有色金属', 22],
  ['000977', '浪潮信息', '计算机', 52],
  ['002463', '沪电股份', '电子', 38],
  ['600584', '长电科技', '电子', 36],
  ['600703', '三安光电', '电子', 14],
  ['002185', '华天科技', '电子', 9.5],
  ['300433', '蓝思科技', '电子', 22],
  ['601127', '赛力斯', '汽车', 128],
  ['002920', '德赛西威', '汽车', 110],
  ['300782', '卓胜微', '电子', 86],
  ['002384', '东山精密', '电子', 18],
  ['300476', '胜宏科技', '电子', 92],
  ['603228', '景旺电子', '电子', 32],
  ['002281', '光迅科技', '通信', 38],
  ['000636', '风华高科', '电子', 16],
  ['300628', '亿联网络', '通信', 38],
  ['600036', '招商银行', '银行', 38],
  ['600900', '长江电力', '公用事业', 27],
  ['601888', '中国中免', '商贸零售', 72],
  ['000568', '泸州老窖', '食品饮料', 142],
  ['002714', '牧原股份', '农林牧渔', 48],
  ['600585', '海螺水泥', '建筑材料', 24],
  ['601166', '兴业银行', '银行', 19],
  ['600809', '山西汾酒', '食品饮料', 188],
  ['002142', '宁波银行', '银行', 26],
  ['300760', '迈瑞医疗', '医药生物', 248],
  ['603501', '韦尔股份', '电子', 98],
  ['002027', '分众传媒', '传媒', 7.2],
  ['600309', '万华化学', '基础化工', 72],
  ['000651', '格力电器', '家用电器', 41],
  ['600406', '国电南瑞', '电力设备', 24],
  ['002049', '紫光国微', '电子', 78],
  ['300496', '中科创达', '计算机', 48],
  ['688111', '金山办公', '计算机', 280],
  ['601225', '陕西煤业', '煤炭', 22],
  ['600438', '通威股份', '电力设备', 22],
  ['002460', '赣锋锂业', '有色金属', 42],
  ['300450', '先导智能', '机械设备', 24],
  ['603986', '兆易创新', '电子', 98],
  ['600031', '三一重工', '机械设备', 18],
]

const STOCKS: StockInfo[] = STOCK_ROWS.map(([code, name, sector, base]) => ({
  code,
  name,
  sector,
  base,
}))

/** 连板记在结束日，展开后才进入各交易日。 */
const STREAKS: Streak[] = [
  ['300750', '2026-10-22', 2, '换手板', '09:41:08', 8.6],
  ['002475', '2026-10-22', 3, 'T字板', '09:32:16', 5.4],
  ['300274', '2026-10-22', 2, '换手板', '09:52:40', 7.8],
  ['002594', '2026-10-22', 1, '换手板', '10:16:22', 6.1],
  ['300059', '2026-10-22', 1, '回封板', '13:18:05', 12.4],
  ['600089', '2026-10-22', 1, '换手板', '10:44:51', 9.2],
  ['601012', '2026-10-22', 1, '烂板', '14:02:33', 16.8],
  ['002371', '2026-10-22', 1, '换手板', '11:05:18', 4.2],
  ['688981', '2026-10-22', 1, '换手板', '10:22:47', 3.9],
  ['000725', '2026-10-22', 1, '换手板', '13:46:09', 8.8],
  ['300502', '2026-10-22', 2, '换手板', '09:37:55', 6.5],
  ['300394', '2026-10-22', 1, '换手板', '10:08:12', 5.1],
  ['002241', '2026-10-22', 1, '回封板', '13:05:44', 11.2],
  ['603799', '2026-10-22', 1, '换手板', '10:55:26', 7.4],
  ['002056', '2026-10-22', 1, '换手板', '11:22:18', 9.6],
  ['300661', '2026-10-22', 1, '一字板', '09:25:01', 2.1],
  ['601669', '2026-10-22', 1, '换手板', null, 8.0],
  ['688012', '2026-10-22', 1, 'T字板', '09:48:27', 4.4],
  ['300308', '2026-10-21', 2, '换手板', '09:36:18', 7.2],
  ['300124', '2026-10-21', 4, '换手板', '09:34:02', 6.8],
  ['688041', '2026-10-21', 2, 'T字板', '09:41:44', 4.6],
  ['002230', '2026-10-21', 1, '换手板', '10:12:09', 9.4],
  ['600276', '2026-10-21', 1, '换手板', '10:28:33', 5.5],
  ['000858', '2026-10-21', 1, '换手板', '13:11:20', 3.8],
  ['603259', '2026-10-21', 1, '回封板', '13:42:55', 8.1],
  ['601138', '2026-10-21', 1, '换手板', '10:06:47', 6.6],
  ['002415', '2026-10-21', 1, '换手板', '11:18:36', 4.9],
  ['000063', '2026-10-21', 1, '换手板', '10:51:14', 7.7],
  ['002074', '2026-10-21', 1, '烂板', '14:11:08', 14.2],
  ['600111', '2026-10-21', 1, '换手板', '10:39:22', 8.5],
  ['300418', '2026-10-21', 1, '回封板', '13:26:41', 13.6],
  ['002466', '2026-10-21', 1, '换手板', '11:02:57', 9.1],
  ['600519', '2026-10-20', 1, '换手板', '10:21:16', 2.4],
  ['000001', '2026-10-20', 1, '换手板', '13:08:44', 3.2],
  ['601318', '2026-10-20', 1, '换手板', '10:47:29', 2.8],
  ['002352', '2026-10-20', 1, '换手板', '11:14:05', 5.6],
  ['300014', '2026-10-20', 2, '换手板', '09:45:33', 8.9],
  ['600030', '2026-10-20', 1, '回封板', '13:33:18', 6.4],
  ['000333', '2026-10-20', 1, '换手板', '10:18:52', 3.5],
  ['601899', '2026-10-20', 1, '换手板', '10:58:07', 7.1],
  ['000977', '2026-10-20', 1, '换手板', '09:57:41', 10.4],
  ['002463', '2026-10-20', 1, 'T字板', '09:35:26', 4.1],
  ['600584', '2026-10-20', 1, '换手板', '11:27:13', 8.3],
  ['600703', '2026-10-19', 1, '换手板', '10:09:18', 7.5],
  ['002185', '2026-10-19', 1, '换手板', '10:41:55', 9.8],
  ['300433', '2026-10-19', 1, '回封板', '13:15:42', 11.6],
  ['601127', '2026-10-19', 1, '换手板', '10:33:07', 6.9],
  ['002920', '2026-10-19', 1, '换手板', '11:08:29', 5.2],
  ['300782', '2026-10-19', 1, '换手板', '09:53:14', 4.7],
  ['002384', '2026-10-16', 1, '换手板', '10:14:36', 8.2],
  ['300476', '2026-10-16', 1, '换手板', '09:49:21', 7.6],
  ['603228', '2026-10-16', 1, '换手板', '10:52:48', 6.3],
  ['002281', '2026-10-16', 1, 'T字板', '09:38:05', 3.4],
  ['000636', '2026-10-16', 1, '烂板', '14:06:17', 15.1],
  ['300628', '2026-10-16', 1, '换手板', '11:11:39', 4.8],
]

export const SCAN_CODES = [
  '600036',
  '600900',
  '601888',
  '000568',
  '002714',
  '600585',
  '601166',
  '600809',
  '002142',
  '300760',
  '603501',
  '002027',
  '600309',
  '000651',
  '600406',
  '002049',
  '300496',
  '688111',
  '601225',
  '600438',
  '002460',
  '300450',
  '603986',
  '600031',
]

const PAIR_BOOST: Record<string, number> = {
  '600809|000858': 1,
  '000568|000858': 2,
  '000651|000333': 3,
  '300450|300124': 4,
  '603986|688981': 5,
  '002049|688012': 6,
  '600036|000001': 7,
  '601166|000001': 8,
  '300496|002415': 9,
  '002460|002466': 10,
  '600438|601012': 11,
  '600406|300124': 12,
  '603501|002371': 13,
  '688111|002230': 14,
}

const TWINS: Record<string, string> = {
  '600809': '000858',
  '000568': '000858',
  '000651': '000333',
  '300450': '300124',
  '603986': '688981',
  '002049': '688012',
  '600036': '000001',
  '601166': '000001',
  '300496': '002415',
  '002460': '002466',
  '600438': '601012',
  '600406': '300124',
  '603501': '002371',
  '688111': '002230',
}

const DAY_COLOR: Record<string, string> = {
  '2026-10-22': '午后分歧加大，烂板与回封同时出现，接力更多集中在容量核心。',
  '2026-10-21': '高位股继续晋级，算力与机器人方向撑住了连板高度。',
  '2026-10-20': '权重票点缀人气，题材与蓝筹同时有涨停，结构还比较散。',
  '2026-10-19': '情绪处于修复段，连板高度还没有完全打开。',
  '2026-10-16': '周末前以首板试错为主，量能没有极端放大。',
}

const byCode = new Map(STOCKS.map((item) => [item.code, item]))

function assertCatalog(): void {
  const codes = STOCKS.map((item) => item.code)
  if (new Set(codes).size !== codes.length) throw new Error('股票代码重复')
  for (const code of [...STREAKS.map((item) => item[0]), ...SCAN_CODES]) {
    if (!byCode.has(code)) throw new Error(`股票目录缺少 ${code}`)
  }
}

assertCatalog()

function hashString(input: string): number {
  let hash = 2166136261
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

function mulberry32(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state |= 0
    state = (state + 0x6d2b79f5) | 0
    let value = Math.imul(state ^ (state >>> 15), 1 | state)
    value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}

function round(value: number, digits = 2): number {
  const power = 10 ** digits
  return Math.round(value * power) / power
}

export function limitPercent(code: string): number {
  if (code.startsWith('300') || code.startsWith('301') || code.startsWith('688') || code.startsWith('689')) {
    return 20
  }
  return 10
}

/** 模拟数据也要给出详情页页眉那三个读数。板块键与中文名照后端 `markets.BOARD_LABELS` 的口径，
 *  否则真假两套数据在同一个页面上会长出两种说法。 */
export function boardKeyOf(code: string): { board: string; board_label: string } {
  if (code.startsWith('300') || code.startsWith('301') || code.startsWith('302')) {
    return { board: 'chinext', board_label: '创业板' }
  }
  if (code.startsWith('688') || code.startsWith('689')) {
    return { board: 'star', board_label: '科创板' }
  }
  return { board: 'main_board', board_label: '主板' }
}

export function boardOf(code: string): 'SH' | 'SZ' {
  return code.startsWith('6') ? 'SH' : 'SZ'
}

export function stock(code: string): StockInfo {
  return (
    byCode.get(code) ?? {
      code,
      name: `股票${code}`,
      sector: '其他',
      base: 10 + (hashString(code) % 8000) / 100,
    }
  )
}

export function tradingDaysEnding(end: string, count: number): string[] {
  const [year, month, day] = end.split('-').map(Number)
  if (!year || !month || !day || count < 1) return []
  const cursor = new Date(Date.UTC(year, month - 1, day))
  const days: string[] = []
  for (let guard = 0; days.length < count && guard < 500; guard += 1) {
    const iso = cursor.toISOString().slice(0, 10)
    const weekday = cursor.getUTCDay()
    if (weekday !== 0 && weekday !== 6 && !CLOSED.has(iso)) days.push(iso)
    cursor.setUTCDate(cursor.getUTCDate() - 1)
  }
  return days.reverse()
}

export function latestTradingDay(date: string): string {
  return tradingDaysEnding(date, 1)[0] ?? date
}

export function poolDates(): string[] {
  return tradingDaysEnding(DATA_DATE, 3)
}

function timeFromHash(code: string, date: string): string {
  const hash = hashString(`${code}|${date}|time`)
  const minutes = 9 * 60 + 35 + (hash % 150)
  const hour = Math.floor(minutes / 60)
  const minute = minutes % 60
  const second = hash % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(hour)}:${pad(minute)}:${pad(second)}`
}

let eventCache: LimitEvent[] | null = null

export function allEvents(): LimitEvent[] {
  if (eventCache) return eventCache
  const map = new Map<string, LimitEvent>()
  for (const [code, end, boards, limitType, time, turnover] of STREAKS) {
    const info = stock(code)
    const days = tradingDaysEnding(end, boards)
    days.forEach((date, index) => {
      const last = index === days.length - 1
      const event: LimitEvent = {
        code,
        name: info.name,
        sector: info.sector,
        board: boardOf(code),
        limit_date: date,
        consecutive_boards: index + 1,
        limit_type: last ? limitType : '换手板',
        first_limit_time: last ? time : timeFromHash(code, date),
        turnover_rate: last ? turnover : round(2 + (hashString(code + date) % 70) / 10, 1),
      }
      const key = `${code}|${date}`
      const previous = map.get(key)
      if (!previous || event.consecutive_boards > previous.consecutive_boards) map.set(key, event)
    })
  }
  eventCache = [...map.values()]
  return eventCache
}

function eventsOn(code: string): Map<string, LimitEvent> {
  const map = new Map<string, LimitEvent>()
  for (const event of allEvents()) {
    if (event.code === code) map.set(event.limit_date, event)
  }
  return map
}

const barCache = new Map<string, KBar[]>()

function buildBars(code: string): KBar[] {
  const info = stock(code)
  const days = tradingDaysEnding(DATA_DATE, HISTORY)
  const random = mulberry32(hashString(code) || 1)
  const limits = eventsOn(code)
  let previous = info.base * (0.9 + random() * 0.12)
  const bars: KBar[] = []
  for (const date of days) {
    const event = limits.get(date)
    const shock = (random() - 0.5) * 0.028
    const pull = ((info.base - previous) / info.base) * 0.08
    const cap = ((limitPercent(code) - 0.5) / 100) * 0.72
    let close = previous * (1 + shock + pull)
    let open = previous * (1 + (random() - 0.5) * 0.012)
    if (!event) {
      close = Math.min(previous * (1 + cap), Math.max(previous * (1 - cap), close))
    } else {
      close = previous * (1 + limitPercent(code) / 100)
      const gap = event.limit_type === '一字板' ? 1 : 0.45 + random() * 0.5
      open = previous * (1 + (limitPercent(code) / 100) * gap)
      if (open > close) open = close
    }
    let high = Math.max(open, close) * (1 + random() * (event ? 0.002 : 0.008))
    let low = Math.min(open, close) * (1 - random() * (event ? 0.004 : 0.008))
    if (event?.limit_type === '一字板') {
      open = close
      high = close
      low = close
    }
    if (event) high = Math.max(high, close)
    const openPrice = round(open)
    const closePrice = round(close)
    let highPrice = round(high)
    let lowPrice = round(low)
    highPrice = Math.max(highPrice, openPrice, closePrice)
    lowPrice = Math.min(lowPrice, openPrice, closePrice)
    const amountTarget = (1.1e8 + random() * 2.6e9) * (event ? 1.55 : 1)
    const volume = Math.max(1000, Math.round(amountTarget / Math.max(closePrice, 0.5)))
    const amount = round((volume * (openPrice + closePrice)) / 2, 2)
    const pct = round(((closePrice / previous - 1) * 100))
    bars.push({
      date,
      open: openPrice,
      high: highPrice,
      low: lowPrice,
      close: closePrice,
      volume,
      amount,
      pct_chg: event ? round(pct, 2) : pct,
      is_limit_up: Boolean(event),
    })
    previous = closePrice
  }
  applyTwin(code, bars)
  return bars
}

function applyTwin(code: string, bars: KBar[]): void {
  const twinCode = TWINS[code]
  if (!twinCode) return
  const event = allEvents()
    .filter((item) => item.code === twinCode && poolDates().includes(item.limit_date))
    .sort((a, b) => (a.limit_date < b.limit_date ? 1 : -1))[0]
  if (!event) return
  const twinWindow = tradingDaysEnding(event.limit_date, 3)
  const targetWindow = tradingDaysEnding(DATA_DATE, 3)
  const twinByDate = new Map(barsFor(twinCode).map((bar) => [bar.date, bar]))
  const indexByDate = new Map(bars.map((bar, index) => [bar.date, index]))
  const cap = (limitPercent(code) - 1.6) / 100
  for (let index = 0; index < targetWindow.length; index += 1) {
    const targetDate = targetWindow[index]
    const twinDate = twinWindow[index]
    const barIndex = targetDate ? indexByDate.get(targetDate) : undefined
    const twin = twinDate ? twinByDate.get(twinDate) : undefined
    if (barIndex == null || barIndex <= 0 || !twin) continue
    const previous = bars[barIndex - 1]
    if (!previous) continue
    const pct = Math.max(-cap, Math.min(cap, (twin.pct_chg / 100) * 0.84))
    const close = round(previous.close * (1 + pct))
    const open = round(previous.close + (close - previous.close) * (twin.close >= twin.open ? 0.32 : 0.7))
    let high = round(Math.max(open, close) * (1 + Math.min(0.015, Math.abs(twin.high / Math.max(twin.close, 0.01) - 1))))
    let low = round(Math.min(open, close) * (1 - Math.min(0.015, Math.abs(1 - twin.low / Math.max(twin.close, 0.01)))))
    high = Math.max(high, open, close)
    low = Math.min(low, open, close)
    const volume = Math.max(1000, Math.round(previous.volume * (0.9 + Math.abs(pct) * 6)))
    bars[barIndex] = {
      date: targetDate,
      open,
      high,
      low,
      close,
      volume,
      amount: round((volume * (open + close)) / 2, 2),
      pct_chg: round((close / previous.close - 1) * 100),
      is_limit_up: false,
    }
  }
}

export function barsFor(code: string): KBar[] {
  const cached = barCache.get(code)
  if (cached) return cached
  const bars = buildBars(code)
  barCache.set(code, bars)
  return bars
}

function toPoolItem(event: LimitEvent): PoolItem {
  const bar = barsFor(event.code).find((item) => item.date === event.limit_date)
  return {
    code: event.code,
    name: event.name,
    board: event.board,
    close: bar?.close ?? 0,
    pct_chg: bar?.pct_chg ?? limitPercent(event.code),
    volume: bar?.volume ?? 0,
    amount: bar?.amount ?? 0,
    turnover_rate: event.turnover_rate,
    consecutive_boards: event.consecutive_boards,
    first_limit_time: event.first_limit_time,
    limit_type: event.limit_type,
    limit_date: event.limit_date,
  }
}

function sortItems(a: PoolItem, b: PoolItem): number {
  if (a.limit_date !== b.limit_date) return a.limit_date < b.limit_date ? 1 : -1
  if (a.consecutive_boards !== b.consecutive_boards) return b.consecutive_boards - a.consecutive_boards
  return a.code < b.code ? -1 : 1
}

export function poolItems(dates = poolDates()): PoolItem[] {
  const allowed = new Set(dates)
  return allEvents()
    .filter((event) => allowed.has(event.limit_date))
    .map(toPoolItem)
    .sort(sortItems)
}

export function limitupsOn(date: string): PoolItem[] {
  const day = latestTradingDay(date)
  return allEvents()
    .filter((event) => event.limit_date === day)
    .map(toPoolItem)
    .sort(sortItems)
}

interface Scores {
  score: number
  shape_score: number
  volume_score: number
  volume_pct_score: number
  other_scores: Record<string, number>
}

function clampScore(value: number): number {
  return round(Math.min(97.5, Math.max(41, value)), 1)
}

function scoreBundle(target: string, match: string, limitDate: string): Scores {
  const hash = hashString(`${target}|${match}|${limitDate}`)
  const unit = (shift: number) => ((hash >>> shift) & 0xff) / 255
  const boost = PAIR_BOOST[`${target}|${match}`] ?? 0
  let shape = 58 + unit(0) * 34
  let volume = 52 + unit(8) * 40
  let volumePct = 54 + unit(16) * 38
  let score = shape * 0.46 + volume * 0.27 + volumePct * 0.27
  if (boost) {
    const day = Number(limitDate.slice(-2))
    score = 96.8 - boost * 0.22 + (Number.isFinite(day) ? day / 400 : 0)
    shape = Math.min(97.4, score + 0.8)
    volume = Math.min(96, score - 1.4)
    volumePct = Math.min(96.5, score - 0.6)
  }
  return {
    score: clampScore(score),
    shape_score: clampScore(shape),
    volume_score: clampScore(volume),
    volume_pct_score: clampScore(volumePct),
    other_scores: {
      amplitude: clampScore(50 + unit(3) * 42 + (boost ? 18 : 0)),
      turnover: clampScore(48 + unit(11) * 44 + (boost ? 12 : 0)),
      body: clampScore(49 + unit(19) * 42 + (boost ? 16 : 0)),
      gap: clampScore(46 + unit(23) * 46 + (boost ? 8 : 0)),
    },
  }
}

export function buildSimilarity(code: string, date: string, top: number, windowSize: number): SimilarityResponse {
  const info = stock(code)
  const day = latestTradingDay(date)
  const size = Math.min(10, Math.max(1, windowSize))
  const dates = poolDates()
  const pool = allEvents().filter((event) => dates.includes(event.limit_date))
  const matches: SimilarityMatch[] = pool
    .filter((event) => event.code !== code)
    .map((event) => ({
      code: event.code,
      name: event.name,
      limit_date: event.limit_date,
      window: tradingDaysEnding(event.limit_date, size),
      ...scoreBundle(code, event.code, event.limit_date),
    }))
    .sort((a, b) => b.score - a.score)
    .slice(0, top)
  return {
    target: { code, name: info.name, window: tradingDaysEnding(day, size) },
    pool_dates: dates,
    pool_size: pool.length,
    disclaimer: '相似度不是涨停概率',
    matches,
  }
}

export function buildScan(date: string, top: number, minScore: number): ScanResponse {
  const day = latestTradingDay(date)
  const dates = poolDates()
  const pool = allEvents().filter((event) => dates.includes(event.limit_date))
  const poolCodes = new Set(pool.map((event) => event.code))
  const items = SCAN_CODES.filter((code) => !poolCodes.has(code))
    .map((code) => {
      let best: { event: LimitEvent; scores: Scores } | null = null
      for (const event of pool) {
        const scores = scoreBundle(code, event.code, event.limit_date)
        if (!best || scores.score > best.scores.score) best = { event, scores }
      }
      if (!best || best.scores.score < minScore) return null
      const info = stock(code)
      const item: ScanItem = {
        code,
        name: info.name,
        score: best.scores.score,
        best_match: {
          code: best.event.code,
          name: best.event.name,
          limit_date: best.event.limit_date,
        },
        shape_score: best.scores.shape_score,
        volume_score: best.scores.volume_score,
        volume_pct_score: best.scores.volume_pct_score,
      }
      return item
    })
    .filter((item): item is ScanItem => item !== null)
    .sort((a, b) => b.score - a.score)
    .slice(0, top)
  return {
    date: day,
    pool_size: pool.length,
    disclaimer: '相似度不是涨停概率',
    items,
  }
}

export function klineOf(code: string, days: number): KlineResponse {
  const info = stock(code)
  const all = barsFor(code)
  const count = Math.min(all.length, Math.max(1, Math.floor(days)))
  const { board, board_label } = boardKeyOf(code)
  return {
    code,
    name: info.name,
    board,
    board_label,
    is_st: info.name.toUpperCase().includes('ST'),
    bars: all.slice(-count),
  }
}

function median(values: (number | null | undefined)[]): number {
  //: 换手、成交额这些列缺就写 null（公开源常整列不给），缺的不参与中位数，别拿 0 顶替。
  const present = values.filter((value): value is number => value != null)
  if (!present.length) return 0
  const sorted = [...present].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  const left = sorted[mid - 1] ?? 0
  const right = sorted[mid] ?? 0
  return sorted.length % 2 ? right : (left + right) / 2
}

export function summaryDates(): string[] {
  return tradingDaysEnding(DATA_DATE, 5)
}

export function buildSummary(date: string, model: string): SummaryDocument {
  const items = allEvents()
    .filter((event) => event.limit_date === date)
    .map(toPoolItem)
  const sectorMap = new Map<string, PoolItem[]>()
  for (const item of items) {
    const sector = stock(item.code).sector
    const list = sectorMap.get(sector) ?? []
    list.push(item)
    sectorMap.set(sector, list)
  }
  const sectors = [...sectorMap.entries()]
    .map(([name, rows]) => ({
      name,
      count: rows.length,
      pct: items.length ? round((rows.length / items.length) * 100, 1) : 0,
    }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, 'zh-CN'))
  const ladderMap = new Map<number, string[]>()
  for (const item of items) {
    const names = ladderMap.get(item.consecutive_boards) ?? []
    names.push(item.name)
    ladderMap.set(item.consecutive_boards, names)
  }
  const ladder = [...ladderMap.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([boards, names]) => ({ boards, count: names.length, examples: names.slice(0, 4) }))
  const patternMap = new Map<string, string[]>()
  for (const item of items) {
    const names = patternMap.get(item.limit_type) ?? []
    names.push(item.name)
    patternMap.set(item.limit_type, names)
  }
  const patterns = [...patternMap.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .map(([name, names]) => ({ name, count: names.length, note: names.slice(0, 3).join('、') }))
  const early = items.filter((item) => (item.first_limit_time ?? '99:99') <= '10:00').length
  const sealed = items.filter((item) => item.limit_type.includes('一字') || item.limit_type.includes('T')).length
  const topSector = sectors[0]
  const secondSector = sectors[1]
  const leader = [...items].sort((a, b) => b.consecutive_boards - a.consecutive_boards)[0]
  const headline = !items.length
    ? `${date} 无涨停样本`
    : `${topSector?.name ?? '市场'}${secondSector ? `与${secondSector.name}` : ''}活跃${
        leader && leader.consecutive_boards > 1 ? `，${leader.name} ${leader.consecutive_boards} 连板` : ''
      }`
  const high = ladder[0]
  const keyPoints = [
    topSector
      ? `涨停 ${items.length} 只，最集中的方向是${topSector.name}（${topSector.count} 只）。`
      : '当日没有涨停样本。',
    high
      ? `连板高度为 ${high.boards <= 1 ? '首板' : `${high.boards} 板`}，共 ${high.count} 只${
          high.examples.length ? `，包括${high.examples.slice(0, 3).join('、')}` : ''
        }。`
      : '当日没有连板结构。',
    `10:00 前封板 ${early} 只，其余多为盘中换手或回封。`,
    DAY_COLOR[date] ?? '结构以换手板为主。',
    '本页是行情结构复盘，相似度不是涨停概率。',
  ]
  return {
    date,
    limitup_count: items.length,
    headline,
    sectors,
    ladder,
    patterns,
    volume_features: {
      median_amount: round(median(items.map((item) => item.amount)), 2),
      median_turnover: round(median(items.map((item) => item.turnover_rate)), 2),
      early_limit_ratio: items.length ? round(early / items.length, 4) : 0,
      sealed_ratio: items.length ? round(sealed / items.length, 4) : 0,
    },
    key_points: keyPoints,
    model,
    generated_at: `${date}T15:45:00+08:00`,
  }
}
