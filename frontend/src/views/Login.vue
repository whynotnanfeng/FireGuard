<template>
  <div class="login-container">
    <div class="login-card fade-in">
      <div class="header">
        <h1 class="brand-title">火灾监测系统</h1>
      </div>

      <a-form :model="form" @keyup.enter="handleSubmit" class="login-form">
        <a-form-item>
          <a-input 
            v-model:value="form.username" 
            placeholder="用户名 / 邮箱" 
            class="underline-input"
            size="large"
          >
            <template #prefix><UserOutlined class="icon-prefix" /></template>
          </a-input>
        </a-form-item>
        <a-form-item>
          <a-input-password 
            v-model:value="form.password" 
            placeholder="密码" 
            class="underline-input"
            size="large"
          >
            <template #prefix><LockOutlined class="icon-prefix" /></template>
          </a-input-password>
        </a-form-item>
        
        <div class="form-actions">
           <a-checkbox v-model:checked="rememberMe" class="remember-me">记住我</a-checkbox>
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
import { UserOutlined, LockOutlined } from '@ant-design/icons-vue'

const authStore = useAuthStore()
const isRegister = ref(false)
const loading = ref(false)
const rememberMe = ref(false)

const form = reactive({
  username: '',
  password: ''
})

async function handleSubmit() {
  if (!form.username || !form.password) {
    message.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    if (isRegister.value) {
      await authStore.register(form.username, form.password)
    } else {
      await authStore.login(form.username, form.password)
    }
  } catch (e) {
    // Error is handled in request interceptor
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
  background-image: radial-gradient(#dfe1e6 1px, transparent 0);
  background-size: 20px 20px;
}

.login-card {
  background: var(--bg-card);
  width: 420px;
  padding: 48px 40px;
  border-radius: 8px;
  box-shadow: 0 10px 30px rgba(9, 30, 66, 0.05);
}

.header {
  text-align: center;
  margin-bottom: 40px;
}

.brand-title {
  color: var(--primary-blue);
  font-size: 32px;
  font-weight: 800;
  letter-spacing: -0.5px;
  margin-bottom: 12px;
}

.brand-subtitle {
  color: var(--text-secondary);
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 4px;
}

.brand-desc {
  color: var(--text-muted);
  font-size: 13px;
}

.login-form {
  margin-bottom: 20px;
}

/* Underline style inputs tailored for Ant Design Vue */
:deep(.underline-input) {
  border-top: none;
  border-left: none;
  border-right: none;
  border-radius: 0;
  border-bottom: 1px solid var(--border-color) !important;
  box-shadow: none !important;
  background: transparent;
  padding-left: 0;
  padding-right: 0;
}

:deep(.underline-input:focus),
:deep(.underline-input-focused) {
  border-bottom-color: var(--primary-blue) !important;
}

:deep(.underline-input .ant-input) {
  background: transparent;
  color: var(--text-primary);
  font-size: 15px;
}

.icon-prefix {
  color: var(--text-muted);
  font-size: 18px;
  margin-right: 8px;
}

.form-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 16px 0 32px 0;
}

.remember-me {
  color: var(--text-secondary);
}

.toggle-mode {
  color: var(--primary-blue);
  font-size: 14px;
  cursor: pointer;
  font-weight: 500;
  transition: opacity 0.3s;
}

.toggle-mode:hover {
  opacity: 0.8;
}

.submit-btn {
  width: 100%;
  height: 44px;
  font-size: 16px;
  border-radius: 4px;
  font-weight: 500;
}

.system-footer {
  margin-top: 40px;
  text-align: center;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 1px;
}

.status-dot {
  width: 8px;
  height: 8px;
  background-color: #5b9bd5;
  border-radius: 50%;
}
</style>
