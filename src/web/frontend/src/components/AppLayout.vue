<template>
  <div class="min-h-screen bg-warm-bg text-gray-800">
    <!-- ============ Sidebar ============ -->
    <!-- h-screen (not h-full): anchor the height to the viewport; the bottom user
         card stays put via shrink-0 while the nav area scrolls on its own. -->
    <aside
      class="fixed top-0 left-0 h-screen bg-warm-bg border-r border-gray-200/60 transition-all duration-300 overflow-hidden"
      :class="[sidebarOpen ? 'w-52' : 'w-0', isNarrow ? 'z-50 shadow-2xl shadow-black/10' : 'z-40']"
    >
      <div class="w-52 h-full flex flex-col p-5">
        <!-- Logo -->
        <div class="flex items-center gap-3 mb-8 shrink-0">
          <div class="w-9 h-9 rounded-xl bg-warm-primary flex items-center justify-center shrink-0">
            <el-icon class="text-white"><Monitor /></el-icon>
          </div>
          <span class="font-semibold text-lg text-gray-800 whitespace-nowrap">DRG 工作台</span>
        </div>

        <!-- Nav (scrollable; scrollbar appears only when menus overflow) -->
        <nav class="space-y-1 flex-1 overflow-y-auto">
          <button
            v-for="item in menus"
            :key="item.id"
            class="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm transition-all duration-200"
            :class="active === item.id
              ? 'bg-warm-primary text-white shadow-lg shadow-warm-primary/25'
              : 'text-gray-500 hover:bg-white hover:text-gray-800 hover:shadow-sm'"
            @click="onNav(item.id)"
          >
            <el-icon class="text-[18px] shrink-0"><component :is="item.icon" /></el-icon>
            <span class="font-medium whitespace-nowrap truncate">{{ item.label }}</span>
            <el-icon v-if="active === item.id" class="ml-auto shrink-0 opacity-60"><ArrowRight /></el-icon>
          </button>
        </nav>

        <!-- Footer user (shrink-0: never squeezed or clipped by the nav area) -->
        <div class="mt-4 pt-4 border-t border-gray-100 shrink-0">
          <div class="flex items-center gap-3 px-1">
            <div class="w-8 h-8 rounded-xl bg-warm-gold flex items-center justify-center text-white text-sm font-semibold shrink-0">
              {{ initial }}
            </div>
            <div class="min-w-0">
              <div class="text-sm font-medium text-gray-700 truncate">{{ me?.name || '—' }}</div>
              <div class="text-xs text-gray-400 truncate">{{ me?.role_name || '' }}</div>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <!-- Narrow screens: sidebar becomes a masked drawer, collapsed by default -->
    <div
      v-if="isNarrow && sidebarOpen"
      class="fixed inset-0 z-40 bg-black/25"
      @click="sidebarOpen = false"
    />

    <!-- ============ Main ============ -->
    <!-- On narrow screens the sidebar is an overlay drawer; content keeps its layout -->
    <div class="transition-all duration-300" :class="sidebarOpen && !isNarrow ? 'ml-52' : 'ml-0'">
      <!-- Top bar -->
      <header class="sticky top-0 z-30 bg-warm-bg/80 backdrop-blur-md border-b border-gray-200/50">
        <div class="flex items-center justify-between gap-4 px-5 md:px-8 py-4">
          <div class="flex items-center gap-4 min-w-0">
            <button
              class="p-2 rounded-xl hover:bg-white hover:shadow-sm transition-all duration-200 shrink-0"
              aria-label="切换侧边栏"
              @click="sidebarOpen = !sidebarOpen"
            >
              <el-icon class="text-gray-500"><Fold /></el-icon>
            </button>
            <div class="min-w-0">
              <h1 class="text-xl font-semibold text-gray-800 truncate">{{ title }}</h1>
              <p class="text-xs text-gray-400 truncate">{{ subtitle }}</p>
            </div>
          </div>

          <div class="flex items-center gap-3 shrink-0">
            <!-- Notification bell (ops alerts are visible to the IT role only) -->
            <div v-if="isIt" class="relative">
              <button
                class="relative p-2.5 rounded-xl bg-white shadow-lg shadow-black/5 hover:shadow-xl transition-all duration-200"
                aria-label="通知"
                @click="showAlerts = !showAlerts"
              >
                <el-icon class="text-gray-600"><Bell /></el-icon>
                <span
                  v-if="openAlerts.length"
                  class="absolute top-1.5 right-1.5 w-2 h-2 bg-warm-terra rounded-full"
                />
              </button>

              <div
                v-if="showAlerts"
                class="absolute right-0 top-full mt-2 w-80 max-w-[calc(100vw-2rem)] bg-white rounded-2xl shadow-2xl shadow-black/10 border border-gray-100 z-50 overflow-hidden"
              >
                <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100">
                  <span class="text-sm font-semibold text-gray-800">告警通知</span>
                  <button class="p-1 rounded-lg hover:bg-gray-100 transition-colors" @click="showAlerts = false">
                    <el-icon class="text-gray-400"><Close /></el-icon>
                  </button>
                </div>
                <div class="divide-y divide-gray-50 max-h-80 overflow-auto">
                  <div
                    v-for="a in openAlerts"
                    :key="a.id"
                    class="flex items-start gap-3 px-5 py-4 hover:bg-warm-bg transition-colors"
                  >
                    <div class="w-9 h-9 rounded-xl bg-warm-terra/10 flex items-center justify-center shrink-0">
                      <el-icon class="text-warm-terra"><WarningFilled /></el-icon>
                    </div>
                    <div class="min-w-0 flex-1">
                      <div class="text-sm font-medium text-gray-800">{{ a.item }}</div>
                      <div class="text-xs text-gray-400 mt-0.5 line-clamp-2">{{ a.detail }}</div>
                    </div>
                  </div>
                  <div v-if="!openAlerts.length" class="px-5 py-6 text-center text-xs text-gray-400">
                    暂无待处理告警
                  </div>
                </div>
              </div>
            </div>

            <!-- Avatar + menu -->
            <div class="relative">
              <button
                class="w-9 h-9 rounded-xl bg-warm-gold flex items-center justify-center text-white text-sm font-semibold hover:shadow-lg transition-all duration-200"
                @click="showUser = !showUser"
              >
                {{ initial }}
              </button>
              <div
                v-if="showUser"
                class="absolute right-0 top-full mt-2 w-52 bg-white rounded-2xl shadow-2xl shadow-black/10 border border-gray-100 z-50 overflow-hidden"
              >
                <div class="px-5 py-4 border-b border-gray-100">
                  <div class="text-sm font-medium text-gray-800">{{ me?.name }}</div>
                  <div class="text-xs text-gray-400 mt-0.5">{{ me?.role_name }} · {{ me?.office || '—' }}</div>
                </div>
                <div class="p-2">
                  <button
                    class="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-gray-600 rounded-xl hover:bg-warm-bg transition-colors"
                    @click="pwdVisible = true; showUser = false"
                  >
                    <el-icon><Lock /></el-icon>修改密码
                  </button>
                  <button
                    class="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-warm-terra rounded-xl hover:bg-warm-bg transition-colors"
                    @click="logout"
                  >
                    <el-icon><SwitchButton /></el-icon>退出登录
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </header>

      <!-- Click-outside overlays -->
      <div v-if="showAlerts || showUser" class="fixed inset-0 z-20" @click="showAlerts = false; showUser = false" />

      <!-- Page content -->
      <main class="p-4 md:p-8"><slot /></main>
    </div>

    <!-- Change password -->
    <el-dialog v-model="pwdVisible" title="修改密码" width="420px" append-to-body>
      <el-form label-width="90px" @submit.prevent>
        <el-form-item label="原密码">
          <el-input v-model="oldPwd" type="password" show-password placeholder="当前密码" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="newPwd" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="newPwd2" type="password" show-password @keyup.enter="submitPwd" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitPwd">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, clearToken } from '../api'

const props = defineProps({
  title: { type: String, default: 'DRG 工作台' },
  subtitle: { type: String, default: '' },
  me: { type: Object, default: () => ({}) },
})
defineEmits(['refresh'])

// Role -> menu mapping (mirrors the backend role guard)
const ROLE_MENU = {
  doctor: [{ id: 'workbench', label: '我的患者', icon: 'User', path: '/workbench' }],
  it: [
    { id: 'ops', label: '控制台', icon: 'Monitor', path: '/ops' },
    { id: 'ops-admin', label: '后台管理', icon: 'Setting', path: '/ops-admin' },
  ],
  finance: [{ id: 'finance', label: '财务 / 收费处', icon: 'Wallet', path: '/finance' }],
  cashier: [{ id: 'finance', label: '财务 / 收费处', icon: 'Wallet', path: '/finance' }],
  manager: [
    { id: 'manager', label: '医务科看板', icon: 'DataAnalysis', path: '/manager' },
    { id: 'mgr-admin', label: '后台管理', icon: 'Setting', path: '/mgr-admin' },
  ],
}

const router = useRouter()
const sidebarOpen = ref(true)
const showAlerts = ref(false)
const showUser = ref(false)
const alerts = ref([])

// Below 1024px the sidebar becomes an overlay drawer, collapsed by default;
// otherwise the fixed sidebar would cover the page content
const isNarrow = ref(false)
function syncViewport() {
  const narrow = window.innerWidth < 1024
  if (narrow === isNarrow.value) return      // act only when crossing the breakpoint
  isNarrow.value = narrow
  sidebarOpen.value = !narrow
}

const menus = computed(() => ROLE_MENU[props.me?.role] || [])
const active = computed(() => (router.currentRoute.value.path || '').replace('/', '') || 'home')
const isIt = computed(() => props.me?.role === 'it')
const openAlerts = computed(() => alerts.value.filter((a) => a.status === 'open'))
const initial = computed(() => (props.me?.name || '?').slice(0, 1))

function onNav(id) {
  const m = menus.value.find((x) => x.id === id)
  if (m && router.currentRoute.value.path !== m.path) router.push(m.path)
  if (isNarrow.value) sidebarOpen.value = false
}

const pwdVisible = ref(false)
const oldPwd = ref('')
const newPwd = ref('')
const newPwd2 = ref('')
const saving = ref(false)

async function submitPwd() {
  if (newPwd.value.length < 6) return ElMessage.warning('新密码至少 6 位')
  if (newPwd.value !== newPwd2.value) return ElMessage.warning('两次输入的新密码不一致')
  saving.value = true
  try {
    await api('/account/password', {
      method: 'POST',
      body: { old_password: oldPwd.value, new_password: newPwd.value },
    })
    ElMessage.success('密码已修改，下次登录请用新密码')
    pwdVisible.value = false
    oldPwd.value = newPwd.value = newPwd2.value = ''
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

function logout() {
  clearToken()
  router.push('/login')
}

// Auto-collapse the drawer on route change on narrow screens
watch(() => router.currentRoute.value.path, () => {
  if (isNarrow.value) sidebarOpen.value = false
})

onMounted(async () => {
  syncViewport()
  window.addEventListener('resize', syncViewport)
  if (isIt.value) {
    try {
      alerts.value = await api('/ops/alerts')
    } catch {
      alerts.value = []
    }
  }
})

onBeforeUnmount(() => window.removeEventListener('resize', syncViewport))
</script>
