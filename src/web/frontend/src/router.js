import { createRouter, createWebHashHistory } from 'vue-router'
import Login from './views/Login.vue'
import DoctorWorkbench from './views/DoctorWorkbench.vue'
import OpsConsole from './views/OpsConsole.vue'
import OpsAdmin from './views/OpsAdmin.vue'
import FinanceWorkbench from './views/FinanceWorkbench.vue'
import ManagerDashboard from './views/ManagerDashboard.vue'
import ManagerAdmin from './views/ManagerAdmin.vue'
import { getToken } from './api'

const routes = [
  { path: '/login', component: Login },
  { path: '/', redirect: '/workbench' },
  { path: '/workbench', component: DoctorWorkbench, meta: { title: '我的患者' } },
  { path: '/ops', component: OpsConsole, meta: { title: '信息科控制台' } },
  { path: '/ops-admin', component: OpsAdmin, meta: { title: '后台管理' } },
  { path: '/finance', component: FinanceWorkbench, meta: { title: '财务/收费处' } },
  { path: '/manager', component: ManagerDashboard, meta: { title: '医务科看板' } },
  { path: '/mgr-admin', component: ManagerAdmin, meta: { title: '后台管理' } }
]

const router = createRouter({ history: createWebHashHistory(), routes })
router.beforeEach((to) => {
  if (to.path !== '/login' && !getToken()) return '/login'
})
export default router
