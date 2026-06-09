import axios from 'axios'
import { message } from 'ant-design-vue'

const request = axios.create({
  baseURL: '/api',
  timeout: 60000,
})

// ── V1.2.66: Global Circuit Breaker for Robust Cleanup ────────────────────────
export const terminatingTasks = new Set<string>()

// ── Request interceptor: attach JWT & implement Circuit Breaker ────────────────
request.interceptors.request.use((config) => {
  // 1. Circuit Breaker: Block requests to tasks that are being deleted
  if (config.url) {
    for (const taskId of terminatingTasks) {
      if (config.url.includes(`/tasks/${taskId}`)) {
        console.warn(`[CircuitBreaker] Blocking request to terminating task: ${taskId}`)
        // Cancel the request immediately
        const controller = new AbortController()
        config.signal = controller.signal
        controller.abort('Task is terminating')
        break
      }
    }
  }

  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

request.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail

    // 避免在登录页时重复跳转导致循环
    if (status === 401) {
      localStorage.removeItem('token')
      const isLoginRequest = err.config?.url?.includes('/auth/login')
      if (!isLoginRequest) {
        window.location.href = '/login'
      }
      return Promise.reject(err)
    }

    const errorMsg =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
        ? detail.map((e: any) => e.msg).join('; ')
        : err.message || '请求失败'

    // 409 和 canceled 错误由调用方自行处理
    if (status !== 409 && !axios.isCancel(err) && err.message !== 'canceled') {
      message.error(errorMsg)
    }
    return Promise.reject(err)
  }
)

export default request
