<template>
  <div class="flex flex-wrap items-center gap-2">
    <button v-for="k in kinds" :key="k.kind" type="button"
      class="inline-flex items-center gap-1.5 px-3.5 py-2 text-sm rounded-xl border border-gray-200
             bg-white text-gray-600 font-medium
             hover:text-warm-primary hover:border-warm-primary/40 hover:shadow-sm
             transition-all duration-200 active:scale-[0.98]
             disabled:opacity-50 disabled:cursor-not-allowed"
      :disabled="busy === k.kind" @click="run(k.kind)">
      <svg v-if="busy !== k.kind" class="w-4 h-4" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v12" /><path d="M7 10l5 5 5-5" /><path d="M5 21h14" />
      </svg>
      <svg v-else class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <path d="M21 12a9 9 0 1 1-6.2-8.5" />
      </svg>
      {{ busy === k.kind ? '导出中…' : k.label }}
    </button>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { downloadExport } from '../api'

const props = defineProps({
  kinds: { type: Array, default: () => [] },   // [{ kind, label }]
})
const busy = ref('')

async function run(kind) {
  busy.value = kind
  try {
    await downloadExport(kind)
    ElMessage.success('报表已导出')
  } catch (e) {
    ElMessage.error(e.message || '导出失败')
  } finally {
    busy.value = ''
  }
}
</script>
