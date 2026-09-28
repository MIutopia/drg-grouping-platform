<template>
  <AppLayout title="我的患者" subtitle="在院患者 · 四行明细" :me="me" @refresh="load">
    <!-- Stats -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 mb-8">
      <WarmStat label="在院患者" :value="count" icon="User" color="bg-warm-primary"
                :hint="snapshot ? `快照 ${snapshot}` : ''" />
      <WarmStat label="中医优势病组" :value="nTcm" icon="FirstAidKit" color="bg-warm-gold" hint="中医路径入组" />
      <WarmStat label="标准 CHS-DRG" :value="nStd" icon="Files" color="bg-warm-slate" hint="常规路径入组" />
      <WarmStat label="暂未产出组码" :value="nNone" icon="Warning" color="bg-warm-terra" hint="需关注" />
    </div>

    <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <div class="flex items-center justify-between mb-5">
        <div>
          <h2 class="text-lg font-semibold text-gray-800">患者清单</h2>
          <p class="text-xs text-gray-400 mt-0.5">点击行查看四行明细（预分组 / 入组差距 / 费用预判 / 优化空间）</p>
        </div>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full">
          <thead>
            <tr class="border-b border-gray-100">
              <th v-for="h in ['住院号', '住院天数', '主要诊断', '预分组', '支付标准', '当前费用', '预计盈亏']" :key="h"
                  class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider whitespace-nowrap">{{ h }}</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="row in items" :key="row.住院号">
              <tr class="border-b border-gray-50 hover:bg-warm-bg transition-colors cursor-pointer"
                  @click="toggle(row.住院号)">
                <td class="py-4 px-4 text-sm font-medium text-gray-800">
                  <span class="inline-flex items-center gap-1.5">
                    <el-icon class="text-gray-300 transition-transform"
                             :class="expanded === row.住院号 ? 'rotate-90' : ''"><ArrowRight /></el-icon>
                    {{ row.住院号 }}
                  </span>
                </td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ row.住院天数 }}</td>
                <td class="py-4 px-4 text-sm text-gray-600 max-w-[16rem] truncate">{{ row.主要诊断 }}</td>
                <td class="py-4 px-4 text-sm">
                  <span class="px-2.5 py-1 text-xs rounded-lg"
                        :class="row.预分组 ? 'bg-warm-primary/10 text-warm-primary' : 'bg-gray-100 text-gray-400'">
                    {{ row.预分组 || '未入组' }}
                  </span>
                </td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ row.支付标准 }}</td>
                <td class="py-4 px-4 text-sm text-gray-500">{{ row.当前费用 }}</td>
                <td class="py-4 px-4 text-sm font-medium"
                    :class="Number(row.预计盈亏) > 0 ? 'text-warm-terra' : 'text-warm-primary'">
                  {{ row.预计盈亏 }}
                </td>
              </tr>
              <tr v-if="expanded === row.住院号" class="bg-warm-bg/60">
                <td colspan="7" class="px-4 py-5">
                  <div class="grid md:grid-cols-2 gap-4">
                    <div v-for="(label, k) in FOUR" :key="k"
                         class="bg-white rounded-xl p-4 shadow-sm shadow-black/[0.03]">
                      <div class="text-xs font-medium text-warm-primary mb-1.5">{{ label }}</div>
                      <div class="text-sm text-gray-600 leading-relaxed">{{ row[k] || '—' }}</div>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div v-if="!items.length" class="py-16 text-center text-sm text-gray-400">当前没有在院患者</div>
    </div>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppLayout from '../components/AppLayout.vue'
import WarmStat from '../components/WarmStat.vue'
import { api } from '../api'

const FOUR = {
  当前预分组: '① 当前预分组',
  入组差距: '② 入组差距',
  费用预判: '③ 费用预判',
  优化空间: '④ 优化空间',
}

const me = ref({})
const items = ref([])
const count = ref(0)
const snapshot = ref('')
const expanded = ref(null)

const nTcm = computed(() => items.value.filter((i) => i['分组路径'] === '中医优势病组规则').length)
const nStd = computed(() => items.value.filter((i) => String(i['分组路径']).startsWith('标准')).length)
const nNone = computed(() => items.value.filter((i) => i['分组路径'] === '未分组').length)

function toggle(no) {
  expanded.value = expanded.value === no ? null : no
}

async function load() {
  try {
    me.value = await api('/me')
    const r = await api('/insim/mine')
    items.value = r.items
    count.value = r.count
    snapshot.value = r.items[0]?.['快照日'] || ''
  } catch (e) {
    ElMessage.error(e.message)
  }
}
onMounted(load)
</script>
