<template>
  <div ref="el" class="w-full" :style="{ height }" />
</template>

<script setup>
/**
 * Generic ECharts chart component (warm design system).
 *
 * type:    bar   vertical bars
 *          line  line chart (with gradient area)
 *          hbar  horizontal bars (for rankings; layout: label | bar | value columns)
 * money:   format values as currency (2 decimals; axis shrinks to 10k units)
 * percent: format values as percentages (axis 0-100 with headroom below the
 *          data lower bound so the trend stays visible)
 * sign:    color by sign (positive = primary teal, negative = terracotta)
 *
 * Bundle size: import echarts/core plus only the charts/components used,
 * never the full package.
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  type: { type: String, default: 'bar' },        // bar | line | hbar
  data: { type: Array, default: () => [] },
  labels: { type: Array, default: () => [] },
  money: { type: Boolean, default: false },
  percent: { type: Boolean, default: false },
  sign: { type: Boolean, default: false },
  color: { type: String, default: '' },
  height: { type: String, default: '16rem' },
  seriesName: { type: String, default: '' },
  labelWidth: { type: Number, default: 0 },      // hbar label column width (0 = auto)
})

const PALETTE = { primary: '#4a9d9a', terra: '#c17767', gold: '#e8b86d', slate: '#6b8e8e' }

const el = ref(null)
let chart = null
let ro = null

function rgba(hex, a) {
  const h = hex.replace('#', '')
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`
}
const AXIS = '#9ca3af'
const GRID_LINE = '#f4f1ec'
const TRACK = '#f3f1ed'
const INK2 = '#4b5563'

function fmtValue(v) {
  if (v === null || v === undefined || v === '') return '—'
  if (props.money) {
    const n = Number(v)
    return isFinite(n) ? n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'
  }
  if (props.percent) {
    const n = Number(v)
    return isFinite(n) ? n.toFixed(1) + '%' : '—'
  }
  return v
}
function axisLabel(v) {
  const n = Number(v)
  if (!isFinite(n)) return ''
  if (props.percent) return n + '%'
  if (props.money && Math.abs(n) >= 10000) return (n / 10000).toFixed(0) + '万'
  return Math.abs(n) >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n)
}

function buildOption() {
  // Missing months arrive as null from the backend: no bar is drawn, tooltip shows "—".
  // Never use Number(v)||0 here — it would turn null into 0, drawing a zero-height bar
  // whose tooltip reads 0.00, indistinguishable from "no data that month" (this is what
  // happens while settlement returns for Jun-Sep have not been imported yet).
  const values = props.data.map(
    (v) => (v === null || v === undefined || v === '' ? null : Number(v) || 0))
  const color = props.color || PALETTE.primary
  const isLine = props.type === 'line'
  const isHBar = props.type === 'hbar'

  const catAxis = {
    type: 'category',
    data: props.labels,
    axisLine: { lineStyle: { color: '#e7e2da' } },
    axisTick: { show: false },
    axisLabel: { color: AXIS, fontSize: 11, hideOverlap: true },
  }
  const numAxis = {
    type: 'value',
    splitLine: { lineStyle: { color: GRID_LINE } },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: AXIS, fontSize: 11, formatter: axisLabel },
    ...(props.percent ? { max: 100, min: (v) => Math.max(0, Math.floor(v.min) - 2) } : {}),
  }

  // Color each point by sign
  const seriesData = props.sign
    ? values.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? PALETTE.primary : PALETTE.terra } }))
    : values

  const barColor = props.sign
    ? undefined
    : { type: 'linear', x: 0, y: 0, x2: isHBar ? 1 : 0, y2: isHBar ? 0 : 1,
        colorStops: [{ offset: 0, color: rgba(color, 0.95) }, { offset: 1, color: rgba(color, 0.55) }] }

  // ---------- Horizontal bars: label column | bar | value column ----------
  // DRG group names are long; without a width cap on axisLabel, containLabel hands the
  // whole plot area to the labels and bars collapse into a sliver. Cap and truncate the
  // left labels, then attach a second category axis on the right as the "value column",
  // producing the three-column layout from the design.
  if (isHBar) {
    const labelW = props.labelWidth || 104
    const valueTexts = values.map((v) => fmtValue(v))
    const valueW = Math.min(120, Math.max(56, Math.max(...valueTexts.map((t) => String(t).length)) * 6.6 + 6))

    return {
      animationDuration: 600,
      grid: { left: 0, right: 0, top: 4, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(74,157,154,0.06)' } },
        backgroundColor: '#ffffff',
        borderColor: '#eee8e0',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: '#374151', fontSize: 12 },
        extraCssText: 'box-shadow:0 8px 24px rgba(0,0,0,0.08);border-radius:12px;',
        formatter: (params) => {
          const p = Array.isArray(params) ? params[0] : params
          return `<div style="font-size:12px"><div style="color:#6b7280;margin-bottom:4px">${p.name}</div>`
            + `<div style="font-weight:600">${fmtValue(p.value)}</div></div>`
        },
      },
      xAxis: { type: 'value', show: false, max: (v) => v.max },
      yAxis: [
        {
          type: 'category',
          data: props.labels,
          inverse: true,
          position: 'left',
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { color: INK2, fontSize: 11, width: labelW, overflow: 'truncate', ellipsis: '…' },
        },
        {
          type: 'category',
          data: valueTexts,
          inverse: true,
          position: 'right',
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: {
            fontSize: 11, fontWeight: 500, width: valueW, overflow: 'truncate',
            color: (v, i) => (props.sign ? (values[i] >= 0 ? PALETTE.primary : PALETTE.terra) : INK2),
          },
        },
      ],
      series: [{
        name: props.seriesName, type: 'bar', data: seriesData, barMaxWidth: 12,
        showBackground: true,
        backgroundStyle: { color: TRACK, borderRadius: 999 },
        itemStyle: { color: barColor, borderRadius: 999 },
      }],
    }
  }

  // ---------- Vertical bars / line ----------
  let series
  if (isLine) {
    series = [{
      // connectNulls=false: break the line on missing months instead of drawing a fake trend across the gap
      name: props.seriesName, type: 'line', data: values, smooth: true, connectNulls: false,
      symbol: 'circle', symbolSize: 8, showSymbol: values.length <= 60,
      lineStyle: { width: 3, color },
      itemStyle: { color, borderColor: '#fff', borderWidth: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: rgba(color, 0.28) }, { offset: 1, color: rgba(color, 0.01) }] } },
    }]
  } else {
    series = [{ name: props.seriesName, type: 'bar', data: seriesData, barMaxWidth: 32,
      itemStyle: { color: barColor, borderRadius: [6, 6, 0, 0] } }]
  }

  return {
    animationDuration: 600,
    grid: { left: 4, right: 12, top: 16, bottom: 4, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: isLine ? 'line' : 'shadow',
        lineStyle: { color: '#d8d2c8' },
        shadowStyle: { color: 'rgba(74,157,154,0.06)' },
      },
      backgroundColor: '#ffffff',
      borderColor: '#eee8e0',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: '#374151', fontSize: 12 },
      extraCssText: 'box-shadow:0 8px 24px rgba(0,0,0,0.08);border-radius:12px;',
      formatter: (params) => {
        const arr = Array.isArray(params) ? params : [params]
        const head = arr[0].axisValue
        const body = arr.map((p) => {
          const name = p.seriesName ? `<span style="color:#9ca3af;margin-right:8px">${p.seriesName}</span>` : ''
          return `<div style="display:flex;align-items:center;gap:8px">${name}<span style="font-weight:600">${fmtValue(p.value)}</span></div>`
        }).join('')
        return `<div style="font-size:12px"><div style="color:#6b7280;margin-bottom:4px">${head}</div>${body}</div>`
      },
    },
    xAxis: catAxis,
    yAxis: numAxis,
    series,
  }
}

function render() {
  if (!el.value) return
  if (!chart) chart = echarts.init(el.value)
  chart.setOption(buildOption(), true)
}

onMounted(async () => {
  await nextTick()
  render()
  // Sidebar collapse and window resizing both change the container width;
  // ResizeObserver tracks that more accurately than window.resize
  ro = new ResizeObserver(() => chart && chart.resize())
  ro.observe(el.value)
})

watch(() => [props.type, props.data, props.labels, props.money, props.percent, props.sign, props.color, props.labelWidth],
  render, { deep: true })

onBeforeUnmount(() => {
  if (ro) ro.disconnect()
  if (chart) { chart.dispose(); chart = null }
})
</script>
