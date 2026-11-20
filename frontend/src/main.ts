import { createApp } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router'
import './style.css'
import { diagLogger } from './utils/diagLogger'

// In development, redirect console output to the backend diag log. Production
// keeps the native console so real runtime errors remain visible to support
// and browser devtools instead of being swallowed into an unflushed buffer.
if (import.meta.env.DEV && typeof window !== 'undefined') {
  console.log = (message?: any, ...optionalParams: any[]) => {
    diagLogger.log('Console', message, ...optionalParams)
  }
  console.warn = (message?: any, ...optionalParams: any[]) => {
    diagLogger.warn('Console', message, ...optionalParams)
  }
  console.error = (message?: any, ...optionalParams: any[]) => {
    diagLogger.error('Console', message, ...optionalParams)
  }
}

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(Antd)

app.mount('#app')

// 心跳：让后端知道还有页面在用这个服务。关闭最后一个页面后后端会自动退出，
// 避免"关掉浏览器但进程一直挂在后台"。`--keep-alive` 启动时后端会忽略心跳。
if (typeof window !== 'undefined' && window.location.protocol.startsWith('http')) {
  const beat = () => {
    fetch('/api/heartbeat', { method: 'POST', keepalive: true }).catch(() => {})
  }
  beat()
  window.setInterval(beat, 15000)
  // 页面卸载（关闭/刷新）时上报一次；刷新后 1 秒内会重新心跳，后端不会误杀。
  window.addEventListener('pagehide', () => {
    try {
      navigator.sendBeacon('/api/heartbeat?leaving=1')
    } catch {
      /* 浏览器不支持时交给超时判定 */
    }
  })
  // 后台标签页的定时器会被限流，切回前台立即补一次。
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) beat()
  })
}

