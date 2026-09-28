<template>
  <AppLayout title="后台管理" subtitle="用户 · 角色 · 科室 · 访问审计" :me="me" @refresh="load">
    <!-- Stats -->
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-5 mb-8">
      <WarmStat label="用户总数" :value="users.length" icon="User" color="bg-warm-primary" />
      <WarmStat label="已启用" :value="activeCount" icon="CircleCheck" color="bg-warm-gold" />
      <WarmStat label="已删除(可撤回)" :value="deletedCount" icon="Delete" color="bg-warm-terra" hint="15 天内可撤回" />
    </div>

    <!-- User management -->
    <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6 mb-6">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-lg font-semibold text-gray-800">用户（{{ users.length }}）</h2>
        <el-button type="primary" @click="openCreate">新增用户</el-button>
      </div>
      <table class="w-full">
        <thead>
          <tr class="border-b border-gray-100">
            <th v-for="h in ['姓名', '手机号', '角色', '科室', '状态', '最近登录', '操作']" :key="h"
                class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.user_id" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
            <td class="py-3 px-4 text-sm font-medium text-gray-800">{{ u.user_name }}</td>
            <td class="py-3 px-4 text-sm text-gray-500">{{ u.login_phone }}</td>
            <td class="py-3 px-4 text-sm text-gray-500">{{ u.role_name }}</td>
            <td class="py-3 px-4 text-sm text-gray-500">{{ u.office }}</td>
            <td class="py-3 px-4">
              <span class="px-2.5 py-1 text-xs rounded-lg" :class="statusClass(u)">{{ statusText(u) }}</span>
            </td>
            <td class="py-3 px-4 text-sm text-gray-400 whitespace-nowrap">{{ fmt(u.last_login_at) }}</td>
            <td class="py-3 px-4 whitespace-nowrap">
              <template v-if="!u.deleted_at">
                <el-button size="small" @click="openEdit(u)">编辑</el-button>
                <el-button size="small" @click="toggleActive(u)">{{ Number(u.active) === 1 ? '停用' : '启用' }}</el-button>
                <el-button size="small" @click="resetPwd(u)">重置密码</el-button>
                <el-button size="small" type="danger" plain @click="remove(u)">删除</el-button>
              </template>
              <el-button v-else size="small" type="success" @click="restore(u)">
                撤回删除（剩 {{ recoverDays(u) }} 天）
              </el-button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Access audit -->
    <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-lg font-semibold text-gray-800">访问审计（{{ audit.length }}）</h2>
        <ExportBar :kinds="[{ kind: 'audit', label: '访问审计' }]" />
      </div>
      <div class="max-h-[24rem] overflow-auto">
        <table class="w-full">
          <thead>
            <tr class="border-b border-gray-100">
              <th v-for="h in ['时间', '用户', '动作', '对象', '详情', 'IP']" :key="h"
                  class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">{{ h }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in audit" :key="a.id" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
              <td class="py-3 px-4 text-sm text-gray-500 whitespace-nowrap">{{ fmt(a.at) }}</td>
              <td class="py-3 px-4 text-sm text-gray-600">{{ a.user_phone }}</td>
              <td class="py-3 px-4 text-sm text-gray-700">{{ a.action }}</td>
              <td class="py-3 px-4 text-sm text-gray-500">{{ a.target }}</td>
              <td class="py-3 px-4 text-sm text-gray-400">{{ a.detail }}</td>
              <td class="py-3 px-4 text-sm text-gray-400">{{ a.ip }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add / edit user -->
    <el-dialog v-model="dlg" :title="editing ? '编辑用户' : '新增用户'" width="440px" append-to-body>
      <el-form :model="form" label-width="80px">
        <el-form-item label="手机号" v-if="!editing">
          <el-input v-model="form.phone" placeholder="11 位手机号（登录名）" />
        </el-form-item>
        <el-form-item label="姓名"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role_code" style="width:100%">
            <el-option v-for="r in roles" :key="r.role_code" :label="r.role_name" :value="r.role_code" />
          </el-select>
        </el-form-item>
        <el-form-item label="科室"><el-input v-model="form.office" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">确定</el-button>
      </template>
    </el-dialog>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppLayout from '../components/AppLayout.vue'
import WarmStat from '../components/WarmStat.vue'
import ExportBar from '../components/ExportBar.vue'
import { api } from '../api'

const me = ref({})
const users = ref([])
const roles = ref([])
const audit = ref([])
const dlg = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = reactive({ user_id: null, phone: '', name: '', role_code: '', office: '' })

const activeCount = computed(() => users.value.filter((u) => !u.deleted_at && Number(u.active) === 1).length)
const deletedCount = computed(() => users.value.filter((u) => u.deleted_at).length)

const fmt = (s) => (s ? String(s).replace('T', ' ').slice(0, 19) : '—')
const statusText = (u) => (u.deleted_at ? '已删除' : (Number(u.active) === 1 ? '启用' : '停用'))
const statusClass = (u) => (u.deleted_at
  ? 'bg-warm-terra/10 text-warm-terra'
  : (Number(u.active) === 1 ? 'bg-warm-primary/10 text-warm-primary' : 'bg-gray-100 text-gray-400'))
const recoverDays = (u) => (u.recover_hours != null ? Math.ceil(Number(u.recover_hours) / 24) : 0)

async function load() {
  try {
    me.value = await api('/me')
    users.value = await api('/ops/users')
    roles.value = await api('/ops/roles')
    audit.value = await api('/ops/audit')
  } catch (e) { ElMessage.error(e.message) }
}

function openCreate() {
  editing.value = false
  Object.assign(form, { user_id: null, phone: '', name: '', role_code: roles.value[0]?.role_code || '', office: '' })
  dlg.value = true
}
function openEdit(u) {
  editing.value = true
  Object.assign(form, { user_id: u.user_id, phone: u.login_phone, name: u.user_name, role_code: u.role_code, office: u.office || '' })
  dlg.value = true
}
async function save() {
  saving.value = true
  try {
    if (editing.value) {
      await api('/ops/users/' + form.user_id, {
        method: 'PATCH',
        body: { user_name: form.name, role_code: form.role_code, office: form.office },
      })
      ElMessage.success('已更新')
    } else {
      const r = await api('/ops/users', {
        method: 'POST',
        body: { phone: form.phone, name: form.name, role_code: form.role_code, office: form.office },
      })
      ElMessage.success('已创建，初始密码 ' + r.init_pwd)
    }
    dlg.value = false
    await load()
  } catch (e) { ElMessage.error(e.message) } finally { saving.value = false }
}
async function toggleActive(u) {
  const to = Number(u.active) === 1 ? 0 : 1
  try {
    await ElMessageBox.confirm(`确认${to === 1 ? '启用' : '停用'}「${u.user_name}」？`, '提示', { type: 'warning' })
    await api('/ops/users/' + u.user_id, { method: 'PATCH', body: { active: to } })
    ElMessage.success('已更新'); await load()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
async function resetPwd(u) {
  try {
    await ElMessageBox.confirm(`将「${u.user_name}」的密码重置为初始密码？`, '提示', { type: 'warning' })
    await api('/ops/users/' + u.user_id, { method: 'PATCH', body: { reset_pwd: true } })
    ElMessage.success('已重置为初始密码'); await load()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
async function remove(u) {
  try {
    await ElMessageBox.confirm(
      `确认删除「${u.user_name}」？删除后 15 天内可在本页撤回，逾期为永久删除。`,
      '删除账号', { type: 'warning', confirmButtonText: '删除' })
    await api('/ops/users/' + u.user_id, { method: 'DELETE' })
    ElMessage.success('已删除（15 天内可撤回）'); await load()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
async function restore(u) {
  try {
    await ElMessageBox.confirm(`确认撤回删除「${u.user_name}」？账号将恢复启用。`, '撤回删除', { type: 'warning' })
    await api('/ops/users/' + u.user_id + '/restore', { method: 'POST' })
    ElMessage.success('已撤回，账号恢复'); await load()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}

onMounted(load)
</script>
