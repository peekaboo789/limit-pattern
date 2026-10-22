export const CHART_FONT = '"WenQuanYi Micro Hei", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'

export const tooltipBase = {
  backgroundColor: 'rgba(8, 12, 20, 0.94)',
  borderColor: 'rgba(232, 184, 109, 0.38)',
  borderWidth: 1,
  textStyle: {
    color: '#e8eef8',
    fontSize: 12,
    fontFamily: CHART_FONT,
  },
  extraCssText: 'border-radius:10px;box-shadow:0 16px 40px rgba(0,0,0,.45);',
  confine: true,
}

export const axisLabel = {
  color: '#93a4bf',
  fontSize: 11,
  fontFamily: CHART_FONT,
}

export const splitLine = {
  lineStyle: { color: 'rgba(148, 176, 214, 0.08)' },
}

export const axisLine = {
  lineStyle: { color: 'rgba(148, 176, 214, 0.22)' },
}
