<template>
  <AppLayout title="信息科控制台" subtitle="告警 · 作业 · 回归 · 数据 · 政策 · 后台" :me="me" @refresh="load">
    <!-- Stats -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 mb-8">
      <WarmStat label="待办告警" :value="ov.open_alerts ?? '—'" icon="Bell"
                :color="ov.open_alerts > 0 ? 'bg-warm-terra' : 'bg-warm-primary'" hint="需确认处理" />
      <WarmStat label="数据新鲜度异常" :value="ov.bad_fresh ?? '—'" icon="Timer"
                :color="ov.bad_fresh > 0 ? 'bg-warm-terra' : 'bg-warm-primary'" hint="滞后 > 24h" />
      <WarmStat label="最近回归判定" :value="ov.last_regress?.verdict === 'pass' ? '通过' : (ov.last_regress?.verdict || '—')"
                icon="CircleCheck" :color="ov.last_regress?.verdict === 'pass' ? 'bg-warm-primary' : 'bg-warm-gold'"
                hint="门槛一致性" />
      <WarmStat label="政策文件登记" :value="ov.policy_docs ?? '—'" icon="Files" color="bg-warm-slate" hint="政策台账" />
    </div>

    <!-- Segmented tabs -->
    <div class="flex flex-wrap gap-1 p-1 bg-gray-100/70 rounded-xl w-fit mb-6">
      <button v-for="t in tabs" :key="t.id"
        class="px-4 py-2 text-sm rounded-lg transition-all duration-200"
        :class="tab === t.id ? 'bg-white text-gray-800 shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
        @click="tab = t.id">{{ t.label }}</button>
    </div>

    <!-- Alert center -->
    <div v-if="tab === 'alerts'" class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-lg font-semibold text-gray-800">告警中心</h2>
        <ExportBar :kinds="[{ kind: 'alerts', label: '告警清单' }]" />
      </div>
      <div class="space-y-3">
        <div v-for="a in alerts" :key="a.id"
             class="flex items-start gap-4 p-4 rounded-xl border border-gray-100 hover:bg-warm-bg transition-colors">
          <div class="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
               :class="a.severity === 'high' ? 'bg-warm-terra/10' : 'bg-warm-gold/10'">
            <el-icon :class="a.severity === 'high' ? 'text-warm-terra' : 'text-warm-gold'"><WarningFilled /></el-icon>
          </div>
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-gray-800">{{ a.item }}</span>
              <span class="px-2 py-0.5 text-[10px] rounded-md"
                    :class="a.severity === 'high' ? 'bg-warm-terra/10 text-warm-terra' : 'bg-warm-gold/10 text-warm-gold'">
                {{ a.severity }}</span>
              <span class="text-xs text-gray-400">{{ a.owner }} · {{ fmt(a.raised_at) }}</span>
            </div>
            <p class="text-xs text-gray-500 mt-1 leading-relaxed">{{ a.detail }}</p>
          </div>
          <el-button v-if="a.status === 'open'" size="small" type="primary" @click="ack(a.id)">确认</el-button>
          <span v-else class="text-xs text-gray-400 shrink-0 self-center">{{ a.acked_by }}</span>
        </div>
        <div v-if="!alerts.length" class="py-16 text-center text-sm text-gray-400">无告警</div>
      </div>
    </div>

    <!-- Job triggers -->
    <div v-else-if="tab === 'jobs'" class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <h2 class="text-lg font-semibold text-gray-800">作业触发</h2>
      <p class="text-xs text-gray-400 mt-0.5 mb-5">全部为白名单作业；写库类已由后端注入写入门禁。运行中每 3 秒自动刷新。</p>
      <div class="flex flex-wrap gap-3 mb-6">
        <el-button v-for="j in jobList" :key="j.key" type="primary" plain
                   :loading="j.running" :disabled="j.running" @click="runJob(j)">{{ j.label }}</el-button>
      </div>
      <table class="w-full">
        <thead>
          <tr class="border-b border-gray-100">
            <th v-for="h in ['开始', '作业', '触发人', '状态', '结束', '']" :key="h"
                class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in jobRuns" :key="r.run_id" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
            <td class="py-3 px-4 text-sm text-gray-500 whitespace-nowrap">{{ r.started_at }}</td>
            <td class="py-3 px-4 text-sm text-gray-700">{{ r.label }}</td>
            <td class="py-3 px-4 text-sm text-gray-500">{{ r.by }}</td>
            <td class="py-3 px-4">
              <span class="px-2.5 py-1 text-xs rounded-lg"
                    :class="r.status === 'ok' ? 'bg-warm-primary/10 text-warm-primary'
                      : (r.status === 'running' ? 'bg-warm-gold/10 text-warm-gold' : 'bg-warm-terra/10 text-warm-terra')">
                {{ r.status }}</span>
            </td>
            <td class="py-3 px-4 text-sm text-gray-500 whitespace-nowrap">{{ r.ended_at || '—' }}</td>
            <td class="py-3 px-4"><el-button size="small" @click="showLog(r)">查看日志</el-button></td>
          </tr>
        </tbody>
      </table>
      <el-dialog v-model="logVisible" title="作业日志" width="80%" append-to-body>
        <pre class="log">{{ logContent || '（无输出）' }}</pre>
      </el-dialog>
    </div>

    <!-- Jobs and regression -->
    <div v-else-if="tab === 'regress'" class="space-y-6">
      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800">回归一致率趋势</h2>
        <p class="text-xs text-gray-400 mt-0.5 mb-6">ADRG 一致率（按运行序 #run_id），判定门槛见下表</p>
        <WarmChart type="line" :data="regRuns.map(r => +(Number(r.adrg_rate) * 100).toFixed(1))"
                   :labels="regRuns.map(r => '#' + r.run_id)" percent series-name="ADRG 一致率"
                   height="15rem" />
      </div>
      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800 mb-5">回归判定</h2>
        <table class="w-full">
          <thead>
            <tr class="border-b border-gray-100">
              <th v-for="h in ['#', '时间', '样本', 'ADRG 一致', '四位码一致', '环比', '判定']" :key="h"
                  class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in regress.runs" :key="r.run_id" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
              <td class="py-3 px-4 text-sm text-gray-400">{{ r.run_id }}</td>
              <td class="py-3 px-4 text-sm text-gray-500 whitespace-nowrap">{{ fmt(r.run_at) }}</td>
              <td class="py-3 px-4 text-sm text-gray-700">{{ r.cases }}</td>
              <td class="py-3 px-4 text-sm text-gray-700">{{ pct(r.adrg_rate) }}</td>
              <td class="py-3 px-4 text-sm text-gray-500">{{ pct(r.drg_rate) }}</td>
              <td class="py-3 px-4 text-sm" :class="Number(r.delta_adrg) < 0 ? 'text-warm-terra' : 'text-warm-primary'">
                {{ Number(r.delta_adrg).toFixed(1) }} pp</td>
              <td class="py-3 px-4">
                <span class="px-2.5 py-1 text-xs rounded-lg"
                      :class="r.verdict === 'pass' ? 'bg-warm-primary/10 text-warm-primary' : 'bg-warm-terra/10 text-warm-terra'">
                  {{ r.verdict }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Data freshness -->
    <div v-else-if="tab === 'fresh'" class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <h2 class="text-lg font-semibold text-gray-800 mb-5">数据新鲜度</h2>
      <table class="w-full">
        <thead>
          <tr class="border-b border-gray-100">
            <th v-for="h in ['数据源', '库', '行数', '滞后(小时)', '业务时间', '状态']" :key="h"
                class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in freshness" :key="r.source_table" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
            <td class="py-3 px-4 text-sm text-gray-700">{{ r.source_table }}</td>
            <td class="py-3 px-4 text-sm text-gray-500">{{ r.source_db }}</td>
            <td class="py-3 px-4 text-sm text-gray-500">{{ r.rows_total }}</td>
            <td class="py-3 px-4 text-sm" :class="Number(r.lag_hours) > 24 ? 'text-warm-terra font-medium' : 'text-gray-500'">
              {{ Number(r.lag_hours).toFixed(2) }}</td>
            <td class="py-3 px-4 text-sm text-gray-400 whitespace-nowrap">{{ fmt(r.business_ts) }}</td>
            <td class="py-3 px-4">
              <span class="px-2.5 py-1 text-xs rounded-lg"
                    :class="r.status === 'ok' ? 'bg-warm-primary/10 text-warm-primary' : 'bg-warm-terra/10 text-warm-terra'">
                {{ r.status }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Policy ledger -->
    <div v-else-if="tab === 'policy'" class="space-y-6">
      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800 mb-5">文件登记（{{ policy.docs.length }}）</h2>
        <div class="max-h-[22rem] overflow-auto">
          <table class="w-full">
            <thead>
              <tr class="border-b border-gray-100">
                <th v-for="h in ['文件', '类别', '格式', '页', '扫描时间']" :key="h"
                    class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in policy.docs" :key="d.doc_key" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
                <td class="py-3 px-4 text-sm text-gray-700">{{ d.file_name }}</td>
                <td class="py-3 px-4 text-sm text-gray-500">{{ d.category }}</td>
                <td class="py-3 px-4 text-sm text-gray-500">{{ d.ext }}</td>
                <td class="py-3 px-4 text-sm text-gray-500">{{ d.pages }}</td>
                <td class="py-3 px-4 text-sm text-gray-400 whitespace-nowrap">{{ fmt(d.captured_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
        <h2 class="text-lg font-semibold text-gray-800 mb-5">抽取的政策事实（{{ policy.facts.length }}）</h2>
        <div class="max-h-[22rem] overflow-auto">
          <table class="w-full">
            <thead>
              <tr class="border-b border-gray-100">
                <th v-for="h in ['事实', '值', '来源文档']" :key="h"
                    class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(f, i) in policy.facts" :key="i" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
                <td class="py-3 px-4 text-sm text-gray-700">{{ f.fact_label }}</td>
                <td class="py-3 px-4 text-sm font-medium text-warm-primary">{{ f.fact_value }}</td>
                <td class="py-3 px-4 text-sm text-gray-400">{{ f.doc_key }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </AppLayout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppLayout from '../components/AppLayout.vue'
import WarmStat from '../components/WarmStat.vue'
import WarmChart from '../components/WarmChart.vue'
import { api } from '../api'
import ExportBar from '../components/ExportBar.vue'

const me = ref({})
const tab = ref('alerts')
const ov = ref({})
const alerts = ref([])
const regress = ref({ runs: [], metrics: [] })
const freshness = ref([])
const policy = ref({ docs: [], facts: [] })
const jobList = ref([])
const jobRuns = ref([])
const logVisible = ref(false)
const logContent = ref('')
let timer = null

const tabs = [
  { id: 'alerts', label: '告警中心' },
  { id: 'jobs', label: '作业触发' },
  { id: 'regress', label: '作业与回归' },
  { id: 'fresh', label: '数据新鲜度' },
  { id: 'policy', label: '政策台账' },
]

const fmt = (s) => (s ? String(s).replace('T', ' ').slice(0, 19) : '—')
const pct = (v) => (v === '' || v == null ? '—' : (Number(v) * 100).toFixed(1) + '%')
const regRuns = computed(() => [...regress.value.runs].reverse())

async function load() {
  try {
    me.value = await api('/me')
    ov.value = await api('/ops/overview')
    alerts.value = await api('/ops/alerts')
    regress.value = await api('/ops/regress')
    freshness.value = await api('/ops/freshness')
    policy.value = await api('/ops/policy')
    await loadJobs()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function ack(id) {
  try { await api('/ops/alerts/' + id + '/ack', { method: 'POST' }); ElMessage.success('已确认'); await load() }
  catch (e) { ElMessage.error(e.message) }
}

async function loadJobs() {
  jobList.value = (await api('/ops/jobs')).jobs
  jobRuns.value = await api('/ops/jobs/runs')
  const anyRunning = jobRuns.value.some((r) => r.status === 'running')
  if (anyRunning && !timer) timer = setInterval(pollJobs, 3000)
  if (!anyRunning && timer) { clearInterval(timer); timer = null }
}
async function pollJobs() {
  jobRuns.value = await api('/ops/jobs/runs')
  jobList.value = (await api('/ops/jobs')).jobs
  if (!jobRuns.value.some((r) => r.status === 'running')) { clearInterval(timer); timer = null }
}
async function runJob(j) {
  try {
    await ElMessageBox.confirm(`确认触发「${j.label}」？`, '触发作业', { type: 'warning' })
    await api('/ops/jobs/run/' + j.key, { method: 'POST' })
    ElMessage.success('已启动，可在下方查看进度')
    await loadJobs()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
function showLog(row) { logContent.value = row.log; logVisible.value = true }

onMounted(load)
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.log {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 12px;
  max-height: 60vh;
  overflow: auto;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
}
</style>
