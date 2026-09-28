<template>
  <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6 hover:shadow-2xl hover:-translate-y-1 transition-all duration-300">
    <div class="flex items-center justify-between mb-4">
      <span class="text-sm text-gray-400">{{ label }}</span>
      <div class="w-9 h-9 rounded-xl flex items-center justify-center shrink-0" :class="color">
        <el-icon class="text-white"><component :is="icon" /></el-icon>
      </div>
    </div>
    <div class="text-2xl font-semibold text-gray-800 mb-1">{{ value }}</div>
    <div v-if="change" class="flex items-center gap-1">
      <el-icon :class="positive ? 'text-warm-primary' : 'text-warm-terra'">
        <component :is="positive ? 'ArrowUp' : 'ArrowDown'" />
      </el-icon>
      <span class="text-xs font-medium" :class="positive ? 'text-warm-primary' : 'text-warm-terra'">{{ change }}</span>
      <span v-if="hint" class="text-xs text-gray-400 ml-1">{{ hint }}</span>
    </div>
    <div v-else-if="hint" class="text-xs text-gray-400">{{ hint }}</div>
  </div>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '—' },
  change: { type: String, default: '' },
  positive: { type: Boolean, default: true },
  hint: { type: String, default: '' },
  icon: { type: String, default: 'DataLine' },
  color: { type: String, default: 'bg-warm-primary' },
})
</script>
