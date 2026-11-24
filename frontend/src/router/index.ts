import { createRouter, createWebHistory } from 'vue-router'

import ClosureDailyReport from '@/views/ClosureDailyReport.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/closure-daily-report', name: 'closure-daily-report', component: ClosureDailyReport },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
