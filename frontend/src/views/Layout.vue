<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="logo">
        <div class="logo-icon">
          <FireOutlined />
        </div>
        <div class="logo-text">
          <h2>FireGuard</h2>
          <span class="logo-sub">Intelligent Fire Monitoring System</span>
        </div>
      </div>
      <nav class="nav-menu">
        <router-link to="/monitor" class="nav-item" active-class="active">
          <span class="nav-icon"><DashboardOutlined /></span>
          <span class="nav-label">Dashboard</span>
        </router-link>
        <router-link to="/tasks" class="nav-item" active-class="active">
          <span class="nav-icon"><UnorderedListOutlined /></span>
          <span class="nav-label">Tasks</span>
        </router-link>
        <router-link to="/models" class="nav-item" active-class="active">
          <span class="nav-icon"><AppstoreOutlined /></span>
          <span class="nav-label">Models</span>
        </router-link>
      </nav>
      <div class="sidebar-footer">
        <div class="version-info">v2.9.0</div>
      </div>
    </aside>

    <div class="main-content">
      <header class="header">
        <div class="page-title">{{ route.path === '/monitor' ? 'Dashboard' : (route.path === '/models' ? 'Models' : 'Tasks') }}</div>
        <div class="user-info">
          <a-dropdown :trigger="['click']">
            <span class="dropdown-link" @click.prevent>
              <UserOutlined /> {{ authStore.user?.username || 'User' }}
            </span>
            <template #overlay>
              <a-menu>
                <a-menu-item key="logout" @click="handleCommand('logout')">Log Out</a-menu-item>
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

      <nav class="mobile-nav">
        <router-link to="/monitor" class="mobile-nav-item" active-class="active">
          <DashboardOutlined />
          <span>Monitor</span>
        </router-link>
        <router-link to="/tasks" class="mobile-nav-item" active-class="active">
          <UnorderedListOutlined />
          <span>Tasks</span>
        </router-link>
        <router-link to="/models" class="mobile-nav-item" active-class="active">
          <AppstoreOutlined />
          <span>Models</span>
        </router-link>
      </nav>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
import { useRoute } from 'vue-router'
import { DashboardOutlined, UnorderedListOutlined, AppstoreOutlined, UserOutlined, FireOutlined } from '@ant-design/icons-vue'

const authStore = useAuthStore()
const route = useRoute()

function handleCommand(cmd: string) {
  if (cmd === 'logout') {
    authStore.logout()
  }
}
</script>

<style scoped>
.sidebar {
  width: 260px;
  background: #ffffff;
  border-right: 1px solid #e8ecf0;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

.logo {
  height: 72px;
  display: flex;
  align-items: center;
  padding: 0 24px;
  gap: 14px;
  border-bottom: 1px solid #e8ecf0;
  position: relative;
  z-index: 1;
}

.logo-icon {
  width: 40px;
  height: 40px;
  background: linear-gradient(135deg, #ff6b35 0%, #f7c948 50%, #ff4444 100%);
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  color: #fff;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(255, 107, 53, 0.3);
}

.logo-text {
  display: flex;
  flex-direction: column;
}

.logo-text h2 {
  font-size: 17px;
  font-weight: 700;
  color: #1a1a2e;
  margin: 0;
  letter-spacing: 0.5px;
  line-height: 1.2;
}

.logo-sub {
  font-size: 10px;
  color: #a0aec0;
  letter-spacing: 1.5px;
  font-weight: 600;
  text-transform: uppercase;
}

.nav-menu {
  padding: 20px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  position: relative;
  z-index: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  color: #64748b;
  text-decoration: none;
  border-radius: 8px;
  transition: all 0.2s ease;
  font-weight: 500;
  font-size: 14px;
  position: relative;
}

.nav-icon {
  font-size: 18px;
  width: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color 0.2s;
}

.nav-label {
  transition: color 0.2s;
}

.nav-item:hover {
  background: #f1f5f9;
  color: #334155;
}

.nav-item.active {
  background: rgba(74, 144, 217, 0.08);
  color: var(--primary-blue);
  font-weight: 600;
}

.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 20px;
  background: var(--primary-blue);
  border-radius: 0 2px 2px 0;
}

.sidebar-footer {
  padding: 16px 24px;
  border-top: 1px solid #e8ecf0;
  position: relative;
  z-index: 1;
}

.version-info {
  font-size: 11px;
  color: #cbd5e1;
  text-align: center;
  letter-spacing: 0.5px;
}

.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-card);
}

.header {
  height: 64px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 28px;
}

.page-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
}

.user-info {
  cursor: pointer;
}

.dropdown-link {
  color: var(--text-secondary);
  transition: color 0.2s;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
}

.dropdown-link:hover {
  color: var(--primary-blue);
}

.content-body {
  flex: 1;
  padding: 24px 28px;
  overflow-y: auto;
  background: var(--bg-primary);
}

.layout {
  display: flex;
  height: 100vh;
  background: var(--bg-card);
}

.mobile-nav {
  display: none;
  height: 64px;
  background: #ffffff;
  border-top: 1px solid #e8ecf0;
  justify-content: space-around;
  align-items: center;
  padding-bottom: env(safe-area-inset-bottom);
}

.mobile-nav-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  color: #64748b;
  text-decoration: none;
  font-size: 12px;
  transition: all 0.2s;
}

.mobile-nav-item span {
  font-size: 10px;
}

.mobile-nav-item.active {
  color: var(--primary-blue);
}

@media (max-width: 992px) {
  .sidebar {
    display: none;
  }
  .mobile-nav {
    display: flex;
  }
  .header {
    padding: 0 16px;
  }
  .content-body {
    padding: 16px;
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
