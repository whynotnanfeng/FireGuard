import { createApp } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router'
import './style.css'
import { diagLogger } from './utils/diagLogger'

// 拦截浏览器 console 打印并重定向至后端日志路径下的 diag.log，浏览器控制台保持纯净不输出
if (typeof window !== 'undefined') {
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

