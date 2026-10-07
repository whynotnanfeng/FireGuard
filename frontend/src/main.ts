import { createApp } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router'
import './style.css'
import { diagLogger } from './utils/diagLogger'

// Intercept browser console output and redirect it to the backend diag.log,
// keeping the browser console completely clean.
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

