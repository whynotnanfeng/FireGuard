<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="logo">
        <h2>火灾监测系统</h2>
      </div>
      <nav class="nav-menu">
        <router-link to="/tasks" class="nav-item" active-class="active">
          <span class="anticon-wrapper"><UnorderedListOutlined /></span> 任务管理
        </router-link>
        <router-link to="/models" class="nav-item" active-class="active">
          <span class="anticon-wrapper"><AppstoreOutlined /></span> 模型库
        </router-link>
      </nav>
    </aside>
    
    <div class="main-content">
      <header class="header">
        <div class="page-title">{{ route.path === '/models' ? '模型库' : '任务管理' }}</div>
        <div class="user-info">
          <a-dropdown :trigger="['click']">
            <span class="dropdown-link" @click.prevent>
              <UserOutlined /> {{ authStore.user?.username || '用户' }}
            </span>
            <template #overlay>
              <a-menu>
                <a-menu-item key="logout" @click="handleCommand('logout')">退出登录</a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </div>
      </header>
      <main class="content-body">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
import { useRoute } from 'vue-router'
import { UnorderedListOutlined, AppstoreOutlined, UserOutlined } from '@ant-design/icons-vue'

const authStore = useAuthStore()
const route = useRoute()

function handleCommand(cmd: string) {
  if (cmd === 'logout') {
    authStore.logout()
  }
}
</script>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
  background: var(--bg-card); /* main content white */
}

/* Sidebar: Very light gray matching reference */
.sidebar {
  width: 240px;
  background: var(--bg-sidebar); 
  border-right: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
}

.logo {
  height: 70px;
  display: flex;
  align-items: center;
  padding: 0 24px;
}

.logo h2 { 
  font-size: 20px; 
  font-weight: 800; 
  color: var(--primary-blue); 
  margin: 0;
  letter-spacing: -0.5px;
}

.nav-menu {
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  color: var(--text-secondary);
  text-decoration: none;
  border-radius: 8px;
  transition: all 0.2s;
  font-weight: 500;
  font-size: 14px;
}

.nav-item:hover {
  background: rgba(9, 30, 66, 0.04);
  color: var(--text-primary);
}

/* Active State: White rect, blue text */
.nav-item.active {
  background: #FFFFFF;
  color: var(--primary-blue);
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}

.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-card);
}

/* Header is clean, matching the white body */
.header {
  height: 70px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 32px;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
}

.user-info {
  cursor: pointer;
}

.el-dropdown-link {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-secondary);
  font-weight: 500;
}

.el-dropdown-link:hover {
  color: var(--primary-blue);
}

.content-body {
  flex: 1;
  padding: 32px;
  overflow-y: auto;
}

/* Transitions */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
