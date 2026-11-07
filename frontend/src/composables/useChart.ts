import { init, type ECharts, type EChartsCoreOption } from '@/charts/register'
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'

export function useChart(build: () => EChartsCoreOption) {
  const el = ref<HTMLDivElement | null>(null)
  const chart = shallowRef<ECharts | null>(null)
  let observer: ResizeObserver | null = null

  function render() {
    chart.value?.setOption(build(), true)
  }

  onMounted(() => {
    if (!el.value) return
    const instance = init(el.value, undefined, { renderer: 'canvas' })
    chart.value = instance
    render()
    observer = new ResizeObserver(() => instance.resize())
    observer.observe(el.value)
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    chart.value?.dispose()
    chart.value = null
  })

  return { el, render }
}
