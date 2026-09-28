<template>
  <AppLayout title="财务 / 收费处" subtitle="结算台账 · 收费对账 · 盈亏看板" :me="me" @refresh="load">
    <!-- Stats -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 mb-8">
      <WarmStat label="本期例数" :value="latest?.cases ?? '—'" icon="User" color="bg-warm-primary"
                :hint="latest?.period || ''" />
      <WarmStat label="总费用" :value="money(latest?.total_cost)" icon="Money" color="bg-warm-slate"
                hint="含全部结算例" />
      <WarmStat label="盈亏" :value="money(latest?.profit_loss)" icon="TrendCharts"
                :color="pl >= 0 ? 'bg-warm-primary' : 'bg-warm-terra'"
                :change="pl >= 0 ? '结余' : '亏损'" :positive="pl >= 0" hint="对支付标准" />
      <WarmStat label="中医组占比" :value="pct(latest?.tcm_share)" icon="FirstAidKit"
                color="bg-warm-gold" hint="中医优势病组" />
    </div>

    <div class="grid lg:grid-cols-3 gap-6 mb-6">
      <div class="lg:col-span-2 bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800">月度盈亏趋势</h2>
        <p class="text-xs text-gray-400 mt-0.5 mb-6">全院结算数据（正=结余 · 负=亏损）</p>
        <WarmChart type="bar" :data="chartData" :labels="chartLabels" money sign height="17rem" />
      </div>
      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800">病组盈亏 Top 10</h2>
        <p class="text-xs text-gray-400 mt-0.5 mb-3">按盈亏排序（元）</p>
        <WarmChart type="hbar" :data="top10Data" :labels="top10Labels" money sign height="19rem" />
      </div>
    </div>

    <!-- Export -->
    <div class="flex items-center justify-between mb-6">
      <p class="text-xs text-gray-400">交材料用 · 一键导出当前视图为 Excel</p>
      <ExportBar :kinds="exportKinds" />
    </div>

    <!-- Segmented tabs -->
    <div class="flex gap-1 p-1 bg-gray-100/70 rounded-xl w-fit mb-6">
      <button v-for="t in tabs" :key="t.id"
        class="px-4 py-2 text-sm rounded-lg transition-all duration-200"
        :class="tab === t.id ? 'bg-white text-gray-800 shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
        @click="tab = t.id">{{ t.label }}</button>
    </div>

    <!-- Panels -->
    <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <div class="overflow-x-auto max-h-[30rem]">
        <table class="w-full">
          <thead>
            <tr class="border-b border-gray-100">
              <th v-for="h in headers[tab]" :key="h"
                  class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider whitespace-nowrap">{{ h }}</th>
            </tr>
          </thead>
          <tbody>
            <!-- DRG profit/loss -->
            <template v-if="tab === 'drg'">
              <tr v-for="r in drgPnl" :key="r.key_code" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
                <td class="py-4 px-4 text-sm font-medium text-gray-800">{{ r.key_name }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ r.cases }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.total_cost) }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.std_cost) }}</td>
                <td class="py-4 px-4 text-sm font-medium" :class="Number(r.profit_loss) >= 0 ? 'text-warm-primary' : 'text-warm-terra'">{{ money(r.profit_loss) }}</td>
                <!-- At the group level use the TCM-advantage flag (suffix F/R/Z), not the
                     share: a group either is TCM or is not, so the share is always
                     100%/0% and carries no information -->
                <td class="py-4 px-4">
                  <span class="px-2.5 py-1 text-xs rounded-lg"
                        :class="Number(r.is_tcm_group) === 1 ? 'bg-warm-primary/10 text-warm-primary' : 'bg-gray-100 text-gray-400'">
                    {{ Number(r.is_tcm_group) === 1 ? '中医' : '非中医' }}
                  </span>
                </td>
              </tr>
            </template>
            <!-- Office profit/loss -->
            <template v-else-if="tab === 'office'">
              <tr v-for="r in officePnl" :key="r.key_code" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
                <td class="py-4 px-4 text-sm font-medium text-gray-800">{{ r.key_name }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ r.cases }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.total_cost) }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.std_cost) }}</td>
                <td class="py-4 px-4 text-sm font-medium" :class="Number(r.profit_loss) >= 0 ? 'text-warm-primary' : 'text-warm-terra'">{{ money(r.profit_loss) }}</td>
                <td class="py-4 px-4 text-sm text-gray-400">{{ pct(r.tcm_share) }}</td>
              </tr>
            </template>
            <!-- Fee reconciliation -->
            <template v-else-if="tab === 'recon'">
              <tr v-for="r in recon.cases" :key="r.medical_no" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
                <td class="py-4 px-4 text-sm text-gray-700">{{ r.medical_no }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ r.drg_code }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.total_cost) }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.his_fee) }}</td>
                <td class="py-4 px-4 text-sm" :class="Math.abs(Number(r.diff)) > 1 ? 'text-warm-terra font-medium' : 'text-gray-500'">{{ money(r.diff) }}</td>
                <td class="py-4 px-4 text-sm text-gray-400 whitespace-nowrap">{{ r.diff_type }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.profit_loss) }}</td>
              </tr>
            </template>
            <!-- Settlement ledger -->
            <template v-else>
              <tr v-for="r in settlement" :key="r.settle_id" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
                <td class="py-4 px-4 text-sm text-gray-700">{{ r.medical_no }}</td>
                <td class="py-4 px-4 text-sm text-gray-500 truncate max-w-[8rem]">{{ r.office_name }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ r.drg_code }}</td>
                <td class="py-4 px-4 text-sm text-gray-600 truncate max-w-[14rem]">{{ r.drg_name }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ r.weight }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.drg_standard) }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ money(r.total_cost) }}</td>
                <td class="py-4 px-4 text-sm font-medium" :class="Number(r.profit_loss) >= 0 ? 'text-warm-primary' : 'text-warm-terra'">{{ money(r.profit_loss) }}</td>
                <td class="py-4 px-4 text-sm text-gray-400">{{ r.actual_days }}</td>
              </tr>
            </template>
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
  { kind: 'settlement', label: '结算台账' },
  { kind: 'recon', label: '收费对账' },
  { kind: 'pnl_drg', label: '病组盈亏' },
  { kind: 'pnl_office', label: '科室盈亏' },
  { kind: 'pnl_month', label: '月度盈亏' },
]

const me = ref({})
const tab = ref('drg')
const periods = ref([])
const latest = ref(null)
const drgPnl = ref([])
const officePnl = ref([])
const recon = ref({ types: [], cases: [] })
const settlement = ref([])

const tabs = [
  { id: 'drg', label: '病组盈亏' },
  { id: 'office', label: '科室盈亏' },
  { id: 'recon', label: '收费对账' },
  { id: 'settle', label: '结算台账' },
]
const headers = {
  drg: ['病组', '例数', '总费用', '支付标准', '盈亏', '中医组'],
  office: ['科室', '例数', '总费用', '支付标准', '盈亏', '中医占比'],
  recon: ['住院号', '组码', '总费用', 'HIS 费用', '差异', '类型', '盈亏'],
  settle: ['住院号', '科室', '组码', '病组', '权重', '支付标准', '总费用', '盈亏', '天数'],
}

const money = (v) => (v === '' || v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const pct = (v) => (v === '' || v == null ? '—' : (Number(v) * 100).toFixed(1) + '%')
const pl = computed(() => Number(latest.value?.profit_loss || 0))
const chartData = computed(() => periods.value.map((p) => Number(p.profit_loss) || 0))
const chartLabels = computed(() => periods.value.map((p) => p.period))
const top10 = computed(() => [...drgPnl.value]
  .sort((a, b) => Number(b.profit_loss) - Number(a.profit_loss))
  .slice(0, 10))
const top10Data = computed(() => top10.value.map((d) => Number(d.profit_loss) || 0))
const top10Labels = computed(() => top10.value.map((d) => d.key_name))

async function load() {
  try {
    me.value = await api('/me')
    const o = await api('/finance/overview')
    periods.value = o.periods
    latest.value = o.latest
    drgPnl.value = await api('/finance/pnl?level=drg')
    officePnl.value = await api('/finance/pnl?level=office')
    recon.value = await api('/finance/recon')
    settlement.value = await api('/finance/settlement?limit=300')
  } catch (e) {
    ElMessage.error(e.message)
  }
}
onMounted(load)
</script>
