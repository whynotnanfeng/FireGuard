import axios from 'axios'
import { message } from 'ant-design-vue'

const request = axios.create({
  baseURL: '/api',
  timeout: 60000,
})

// ── Request interceptor: attach JWT ──────────────────────────────────────────
request.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ── Response interceptor: handle errors ──────────────────────────────────────
request.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail

    if (status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
      return Promise.reject(err)
    }

    const errorMsg =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
        ? detail.map((e: any) => e.msg).join('; ')
        : err.message || '请求失败'

    message.error(errorMsg)
    return Promise.reject(err)
  }
)

export default request
