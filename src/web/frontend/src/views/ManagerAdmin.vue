<template>
  <AppLayout title="后台管理" subtitle="结算清单上传 · 白名单审议" :me="me">
    <!-- DRG settlement list upload (owned by the medical affairs office) -->
    <div class="bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <h2 class="text-lg font-semibold text-gray-800 mb-1">DRG 医保结算清单上传</h2>
      <p class="text-xs text-gray-400 mb-6 leading-relaxed">
        上传当月医保结算清单到 <span class="font-medium text-gray-600">原始资料 / DRG结算样表</span>，
        下次「结算导入」作业自动拾取。支持 xlsx / xls / csv / pdf / docx，单文件 ≤ 50MB。
      </p>
      <div class="border-2 border-dashed border-gray-200 rounded-2xl p-8 text-center
                  hover:border-warm-primary/40 transition-colors">
        <input ref="fileInput" type="file" class="hidden"
               accept=".xlsx,.xls,.csv,.pdf,.docx,.doc" @change="onFilePick" />
        <button type="button" @click="fileInput?.click()"
          class="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-warm-primary text-white
                 text-sm font-medium shadow-sm hover:shadow-md hover:brightness-105
                 active:scale-[0.98] transition-all">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4" /><path d="M7 9l5-5 5 5" /><path d="M5 20h14" /></svg>
          选择文件
        </button>
        <p v-if="uploadFile" class="mt-4 text-sm text-gray-600 break-all">{{ uploadFile.name }}</p>
        <p v-else class="mt-4 text-xs text-gray-400">点击上方按钮选择要上传的结算清单文件</p>
      </div>
      <div class="mt-5 flex items-center gap-3">
        <el-button type="primary" :disabled="!uploadFile" :loading="uploading" @click="doUpload">上传</el-button>
        <span v-if="uploadMsg" class="text-sm" :class="uploadOk ? 'text-warm-primary' : 'text-warm-terra'">{{ uploadMsg }}</span>
      </div>
    </div>

    <!-- Whitelist review (add / delete / review) -->
    <div class="mt-6 bg-white rounded-2xl shadow-xl shadow-black/[0.04] p-6">
      <div class="flex items-center justify-between mb-5">
        <div>
          <h2 class="text-lg font-semibold text-gray-800">白名单审议</h2>
          <p class="text-xs text-gray-400 mt-0.5">共 {{ wlRows.length }} 条，仅「已确认」在系统输出中生效</p>
        </div>
        <el-button type="primary" @click="addVisible = true">新增白名单</el-button>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full">
          <thead>
            <tr class="border-b border-gray-100">
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">规则编号</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">类别</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">规则内容</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">状态</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">审议人 / 日期</th>
              <th class="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase tracking-wider">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in wlRows" :key="r.rule_id" class="border-b border-gray-50 hover:bg-warm-bg transition-colors">
              <td class="py-3 px-4 text-sm font-medium text-gray-800">{{ r.rule_id }}</td>
              <td class="py-3 px-4 text-sm text-gray-500">{{ r.category }}</td>
              <td class="py-3 px-4 text-sm text-gray-600 max-w-md">{{ r.rule_text }}</td>
              <td class="py-3 px-4">
                <span class="px-2.5 py-1 text-xs rounded-lg" :class="statusClass(r.status)">{{ r.status }}</span>
              </td>
              <td class="py-3 px-4 text-xs text-gray-400">{{ r.confirmed_by || '—' }}<span v-if="r.confirmed_date"> · {{ r.confirmed_date }}</span></td>
              <td class="py-3 px-4 whitespace-nowrap">
                <button v-if="r.status !== '已确认'" class="text-sm text-warm-primary hover:underline mr-3" @click="review(r, '已确认')">审核</button>
                <button v-else class="text-sm text-gray-400 hover:underline mr-3" @click="review(r, '待审议')">撤回</button>
                <button class="text-sm text-warm-terra hover:underline" @click="remove(r)">删除</button>
              </td>
            </tr>
            <tr v-if="!wlRows.length"><td colspan="6" class="py-8 text-center text-sm text-gray-400">暂无白名单规则</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add whitelist dialog -->
    <el-dialog v-model="addVisible" title="新增白名单" width="480px" append-to-body>
      <el-form label-width="80px" @submit.prevent>
        <el-form-item label="规则编号">
          <el-input v-model="form.rule_id" placeholder="如 W19" />
        </el-form-item>
        <el-form-item label="类别">
          <el-input v-model="form.category" placeholder="如 输出合规" />
        </el-form-item>
        <el-form-item label="规则内容">
          <el-input v-model="form.rule_text" type="textarea" :rows="3" placeholder="规则原文 / 触发条件 / 政策出处" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitAdd">确定</el-button>
      </template>
    </el-dialog>
  </AppLayout>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppLayout from '../components/AppLayout.vue'
import { api, getToken, clearToken } from '../api'

const me = ref({})
const wlRows = ref([])
const fileInput = ref(null)
const uploadFile = ref(null)
const uploading = ref(false)
const uploadMsg = ref('')
const uploadOk = ref(false)

const addVisible = ref(false)
const saving = ref(false)
const form = ref({ rule_id: '', category: '', rule_text: '' })

const statusClass = (s) => s === '已确认'
  ? 'bg-warm-primary/10 text-warm-primary'
  : s === '已否决' ? 'bg-warm-terra/10 text-warm-terra'
    : 'bg-gray-100 text-gray-400'

async function load() {
  try {
    me.value = await api('/me')
    wlRows.value = await api('/manager/whitelist')
  } catch (e) {
    ElMessage.error(e.message)
  }
}
onMounted(load)

// ---- settlement list upload ----
function onFilePick(e) {
  uploadFile.value = e.target.files?.[0] || null
  uploadMsg.value = ''
}
async function doUpload() {
  if (!uploadFile.value) return
  uploading.value = true
  uploadMsg.value = ''
  try {
    const fd = new FormData()
    fd.append('file', uploadFile.value)
    const headers = {}
    const t = getToken()
    if (t) headers.Authorization = 'Bearer ' + t
    const res = await fetch('/api/manager/upload', { method: 'POST', headers, body: fd })
    if (res.status === 401) { clearToken(); location.hash = '#/login'; throw new Error('登录已失效') }
    if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.detail || res.statusText) }
    const j = await res.json()
    uploadOk.value = true
    uploadMsg.value = `已上传：${j.saved}（${Math.round(j.size / 1024)} KB）`
    ElMessage.success('上传成功')
    uploadFile.value = null
    if (fileInput.value) fileInput.value.value = ''
  } catch (e) {
    uploadOk.value = false
    uploadMsg.value = e.message || '上传失败'
    ElMessage.error(e.message || '上传失败')
  } finally {
    uploading.value = false
  }
}

// ---- whitelist review ----
async function submitAdd() {
  if (!form.value.rule_id.trim() || !form.value.rule_text.trim()) return ElMessage.warning('请填写规则编号与内容')
  saving.value = true
  try {
    await api('/manager/whitelist', {
      method: 'POST',
      body: { rule_id: form.value.rule_id.trim(), category: form.value.category.trim(), rule_text: form.value.rule_text.trim() },
    })
    ElMessage.success('已新增（待审议）')
    addVisible.value = false
    form.value = { rule_id: '', category: '', rule_text: '' }
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function review(r, status) {
  try {
    await api(`/manager/whitelist/${encodeURIComponent(r.rule_id)}`, { method: 'PUT', body: { status } })
    ElMessage.success(status === '已确认' ? '已审核确认' : '已撤回为待审议')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function remove(r) {
  try {
    await ElMessageBox.confirm(`确认删除白名单 ${r.rule_id}？`, '删除', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api(`/manager/whitelist/${encodeURIComponent(r.rule_id)}`, { method: 'DELETE' })
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>
