<template>
  <div class="login-container">
    <div class="login-card fade-in">
      <div class="header">
        <div class="logo-icon">
          <FireOutlined />
        </div>
        <h1 class="brand-title">火灾监测系统</h1>
        <p class="brand-subtitle">FIRE DETECTION SYSTEM</p>
      </div>

      <a-form :model="form" @keyup.enter="handleSubmit" class="login-form">
        <a-form-item>
          <a-input 
            v-model:value="form.username" 
            placeholder="用户名" 
            class="login-input"
            size="large"
          >
            <template #prefix><UserOutlined class="icon-prefix" /></template>
          </a-input>
        </a-form-item>
        <a-form-item>
          <a-input-password 
            v-model:value="form.password" 
            placeholder="密码" 
            class="login-input"
            size="large"
          >
            <template #prefix><LockOutlined class="icon-prefix" /></template>
          </a-input-password>
        </a-form-item>
        
        <div class="form-actions">
           <span class="toggle-mode" @click="isRegister = !isRegister">
            {{ isRegister ? '去登录' : '没有账号？注册' }}
           </span>
        </div>

        <a-button 
          type="primary" 
          class="submit-btn" 
          :loading="loading" 
          @click="handleSubmit"
        >
          {{ isRegister ? '注册并登录' : '登 录' }}
        </a-button>
        
      </a-form>

      <div class="system-footer">
         <span class="status-dot"></span>
         SYSTEM ONLINE
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { message } from 'ant-design-vue'
import { UserOutlined, LockOutlined, FireOutlined } from '@ant-design/icons-vue'

const authStore = useAuthStore()
const isRegister = ref(false)
const loading = ref(false)

const form = reactive({
  username: '',
  password: ''
})

async function handleSubmit() {
  console.log('[Login] handleSubmit triggered', form.username)
  if (!form.username || !form.password) {
    message.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    if (isRegister.value) {
      await authStore.register(form.username, form.password)
      message.success('注册成功')
    } else {
      await authStore.login(form.username, form.password)
      message.success('欢迎回来')
    }
  } catch (e: any) {
    console.error('Login error details:', e)
    
    let errorMsg = '登录失败，请检查网络连接'
    
    if (e.response) {
      const status = e.response.status
      const detail = e.response.data?.detail
      
      if (status === 401) {
        errorMsg = '用户名或密码错误'
      } else if (status === 400) {
        errorMsg = typeof detail === 'string' ? detail : '请求参数错误'
      } else if (status === 500) {
        errorMsg = '服务器内部错误，请联系管理员'
      } else if (detail) {
        errorMsg = typeof detail === 'string' ? detail : JSON.stringify(detail)
      }
    } else if (e.message) {
      errorMsg = e.message
    }
    
    message.error(errorMsg)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-primary);
}

.login-card {
  background: var(--bg-card);
  width: 100%;
  max-width: 380px;
  padding: 40px 32px;
  border-radius: 12px;
  box-shadow: var(--shadow-card);
  border: 1px solid var(--border-color);
  margin: 0 16px;
}

.header {
  text-align: center;
  margin-bottom: 36px;
}

.logo-icon {
  width: 52px;
  height: 52px;
  background: linear-gradient(135deg, #ff6b35 0%, #f7c948 50%, #ff4444 100%);
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  color: #fff;
  margin: 0 auto 16px;
  box-shadow: 0 4px 14px rgba(255, 107, 53, 0.25);
}

.brand-title {
  color: var(--text-primary);
  font-size: 24px;
  font-weight: 700;
  letter-spacing: 1px;
  margin: 0 0 6px 0;
}

.brand-subtitle {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 2px;
  text-transform: uppercase;
  margin: 0;
}

.login-form {
  margin-bottom: 0;
}

:deep(.login-input) {
  border-radius: 8px;
  border: 1px solid var(--border-color);
  background: var(--bg-input);
  transition: all 0.2s;
}

:deep(.login-input:hover) {
  border-color: var(--primary-blue);
}

:deep(.login-input:focus-within) {
  border-color: var(--primary-blue);
  box-shadow: 0 0 0 3px rgba(74, 144, 217, 0.1);
}

:deep(.login-input .ant-input) {
  background: transparent;
  color: var(--text-primary);
  font-size: 14px;
}

.icon-prefix {
  color: var(--text-muted);
  font-size: 16px;
  margin-right: 8px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  margin: 16px 0 24px 0;
}

.toggle-mode {
  color: var(--primary-blue);
  font-size: 13px;
  cursor: pointer;
  font-weight: 500;
  transition: opacity 0.2s;
}

.toggle-mode:hover {
  opacity: 0.8;
}

.submit-btn {
  width: 100%;
  height: 42px;
  font-size: 15px;
  border-radius: 8px;
  font-weight: 600;
  letter-spacing: 1px;
}

.system-footer {
  margin-top: 32px;
  text-align: center;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 1.5px;
}

.status-dot {
  width: 6px;
  height: 6px;
  background-color: var(--success-green);
  border-radius: 50%;
}
</style>
