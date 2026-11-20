import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

// Lazy-loaded views
const Login = () => import('@/views/Login.vue')
const Layout = () => import('@/views/Layout.vue')
const TaskList = () => import('@/views/TaskList.vue')
const ModelList = () => import('@/views/ModelList.vue')
const MonitorDashboard = () => import('@/views/MonitorDashboard.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      component: Login,
      meta: { public: true },
    },
    {
      path: '/',
      component: Layout,
      children: [
        { path: '', redirect: '/tasks' },
        { path: 'monitor', component: MonitorDashboard },
        { path: 'tasks', component: TaskList },
        { path: 'models', component: ModelList },
      ],
    },
  ],
})

// Navigation guard
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isLoggedIn) {
    return '/login'
  }
  if (to.path === '/login' && auth.isLoggedIn) {
    return '/tasks'
  }
})

export default router
