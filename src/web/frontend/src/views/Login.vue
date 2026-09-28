<template>
  <div class="min-h-screen bg-warm-bg flex items-center justify-center p-4">
    <div class="w-full max-w-sm">
      <div class="flex flex-col items-center mb-8">
        <div class="w-14 h-14 rounded-2xl bg-warm-primary flex items-center justify-center shadow-lg shadow-warm-primary/25 mb-4">
          <el-icon class="text-white" :size="26"><Monitor /></el-icon>
        </div>
        <h1 class="text-2xl font-semibold text-gray-800">DRG 工作台</h1>
        <p class="text-sm text-gray-400 mt-1.5">示例中医医院 · 多角色平台</p>
      </div>

      <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-8">
        <el-form @submit.prevent="submit">
          <el-form-item>
            <el-input v-model="phone" size="large" placeholder="手机号" :prefix-icon="Iphone" />
          </el-form-item>
          <el-form-item>
            <el-input v-model="password" size="large" type="password" show-password
                      placeholder="密码（初始口令由信息科下发）" :prefix-icon="Lock" @keyup.enter="submit" />
          </el-form-item>
          <el-button type="primary" size="large" class="w-full" :loading="loading" @click="submit">
            登 录
          </el-button>
        </el-form>
      </div>

      <p class="text-center text-xs text-gray-400 mt-6">首次登录请使用人事登记的手机号</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Iphone, Lock } from '@element-plus/icons-vue'
import { api, setToken } from '../api'

const router = useRouter()
const phone = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!phone.value || !password.value) return ElMessage.warning('请输入手机号和密码')
  loading.value = true
  try {
    const r = await api('/login', { method: 'POST', body: { phone: phone.value, password: password.value } })
    setToken(r.token)
    const me = await api('/me')            // route by role
    ElMessage.success('欢迎，' + me.name)
    const home = { it: '/ops', finance: '/finance', cashier: '/finance', manager: '/manager' }[me.role] || '/workbench'
    router.push(home)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
</script>
