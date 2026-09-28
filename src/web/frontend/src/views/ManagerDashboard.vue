<template>
  <AppLayout title="医务科看板" subtitle="全院盈亏 · 对表一致率 · 白名单审议" :me="me">
    <!-- Stats -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 mb-8">
      <WarmStat label="本期例数" :value="latest?.cases ?? '—'" icon="User"
                color="bg-warm-primary" :hint="latest?.period || ''" />
      <WarmStat label="盈亏" :value="money(latest?.profit_loss)" icon="TrendCharts"
                :color="pl >= 0 ? 'bg-warm-primary' : 'bg-warm-terra'"
                :change="pl >= 0 ? '盈利' : '亏损'" :positive="pl >= 0" hint="较支付标准" />
      <WarmStat label="对表 ADRG 一致" :value="pct(cmp.adrg_rate)" icon="CircleCheck"
                color="bg-warm-gold" :hint="`${cmp.adrg_ok || 0} / ${cmp.n || 0} 例`" />
      <WarmStat label="中医组占比" :value="pct(latest?.tcm_share)" icon="FirstAidKit"
                color="bg-warm-slate" hint="中医优势病组" />
    </div>

    <div class="grid lg:grid-cols-3 gap-6">
      <!-- Bar chart -->
      <div class="lg:col-span-2 bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800">月度盈亏趋势</h2>
        <p class="text-xs text-gray-400 mt-0.5 mb-6">全院结算数据（正=结余 · 负=亏损）</p>
        <WarmChart type="bar" :data="chartData" :labels="chartLabels" money sign height="17rem" />
      </div>

      <!-- Status breakdown -->
      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800">全院状态</h2>
        <p class="text-xs text-gray-400 mt-0.5 mb-5">关键质量指标</p>
        <div class="space-y-4">
          <div v-for="m in metrics" :key="m.label">
            <div class="flex justify-between text-sm mb-1.5">
              <span class="text-gray-600">{{ m.label }}</span>
              <span class="font-medium text-gray-800">{{ m.value }}</span>
            </div>
            <div class="h-2 bg-gray-100 rounded-full overflow-hidden">
              <div
                class="h-full rounded-full transition-all duration-500"
                :style="{ width: m.pct + '%', backgroundColor: m.color }"
              />
            </div>
          </div>
        </div>

        <div class="mt-6 pt-5 border-t border-gray-100 flex items-center gap-2 flex-wrap">
          <span
            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-xl"
            :class="reg?.verdict === 'pass' ? 'bg-warm-primary/10 text-warm-primary' : 'bg-warm-terra/10 text-warm-terra'"
          >
            <span class="w-1.5 h-1.5 rounded-full" :class="reg?.verdict === 'pass' ? 'bg-warm-primary' : 'bg-warm-terra'" />
            回归判定 {{ reg?.verdict === 'pass' ? '通过' : (reg?.verdict || '—') }}
          </span>
          <span class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-xl bg-warm-gold/10 text-warm-gold">
            <span class="w-1.5 h-1.5 rounded-full bg-warm-gold" />
            白名单 {{ wl['已确认'] || 0 }}/{{ wl.total || 0 }} 已签认
          </span>
        </div>
      </div>
    </div>

    <!-- DRG table -->
    <div class="mt-6 bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <div class="flex items-center justify-between mb-5">
        <div>
          <h2 class="text-lg font-semibold text-gray-800">病组盈亏</h2>
          <p class="text-xs text-gray-400 mt-0.5">共 {{ pnlRows.length }} 个病组</p>
        </div>
        <ExportBar :kinds="exportKinds" />
      </div>
      <div class="overflow-x-auto max-h-[28rem]">
        <table class="w-full">
          <thead>
            <tr class="border-b border-gray-100">
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">病组</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">例数</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">总费用</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">支付标准</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">盈亏</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">中医组</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in pnlRows" :key="row.key_code" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
              <td class="py-4 px-4 text-sm font-medium text-gray-800">{{ row.key_name }}</td>
              <td class="py-4 px-4 text-sm text-gray-500">{{ row.cases }}</td>
              <td class="py-4 px-4 text-sm text-gray-500">{{ money(row.total_cost) }}</td>
              <td class="py-4 px-4 text-sm text-gray-500">{{ money(row.std_cost) }}</td>
              <td class="py-4 px-4 text-sm font-medium"
                  :class="Number(row.profit_loss) >= 0 ? 'text-warm-primary' : 'text-warm-terra'">
                {{ money(row.profit_loss) }}
              </td>
              <td class="py-4 px-4">
                <span class="px-2.5 py-1 text-xs rounded-lg"
                      :class="Number(row.is_tcm_group) === 1 ? 'bg-warm-primary/10 text-warm-primary' : 'bg-gray-100 text-gray-400'">
                  {{ Number(row.is_tcm_group) === 1 ? '中医' : '非中医' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppLayout from '../components/AppLayout.vue'
import WarmStat from '../components/WarmStat.vue'
import WarmChart from '../components/WarmChart.vue'
import ExportBar from '../components/ExportBar.vue'
import { api } from '../api'

const exportKinds = [
  { kind: 'compare', label: '对表明细' },
  { kind: 'pnl_drg', label: '病组盈亏' },
  { kind: 'pnl_office', label: '科室盈亏' },
  { kind: 'pnl_month', label: '月度盈亏' },
]

const me = ref({})
const periods = ref([])
const latest = ref(null)
const cmp = ref({})
const paths = ref([])
const wl = ref({})
const reg = ref({})
const pnlRows = ref([])

const money = (v) => (v === '' || v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const pct = (v) => (v === '' || v == null ? '—' : (Number(v) * 100).toFixed(1) + '%')
const pl = computed(() => Number(latest.value?.profit_loss || 0))

const chartData = computed(() => periods.value.map((p) => Number(p.profit_loss) || 0))
const chartLabels = computed(() => periods.value.map((p) => p.period))

const metrics = computed(() => {
  const pathsTotal = paths.value.reduce((s, p) => s + Number(p.n || 0), 0)
  const tcm = paths.value.find((p) => p.path === 'tcm')?.n || 0
  return [
    { label: '对表 ADRG 一致', value: pct(cmp.value.adrg_rate), pct: (Number(cmp.value.adrg_rate || 0) * 100).toFixed(1), color: '#4a9d9a' },
    { label: '四位码一致', value: pct(cmp.value.drg_rate), pct: (Number(cmp.value.drg_rate || 0) * 100).toFixed(1), color: '#e8b86d' },
    {
      label: '中医组病例占比',
      value: pct(pathsTotal ? tcm / pathsTotal : 0),
      pct: (pathsTotal ? (tcm / pathsTotal) * 100 : 0).toFixed(1),
      color: '#6b8e8e',
    },
  ]
})

async function load() {
  try {
    me.value = await api('/me')
    const o = await api('/manager/overview')
    periods.value = o.periods
    latest.value = o.latest
    cmp.value = o.compare
    paths.value = o.paths
    wl.value = o.whitelist
    reg.value = o.last_regress || {}
    pnlRows.value = await api('/manager/pnl?level=drg')
  } catch (e) {
    ElMessage.error(e.message)
  }
}
onMounted(load)</script>
