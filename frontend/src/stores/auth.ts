import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi, type UserRes } from '@/api/auth'
import router from '@/router'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user = ref<UserRes | null>(null)

  const isLoggedIn = computed(() => !!token.value)

  async function login(username: string, password: string) {
    const res = await authApi.login({ username, password })
    token.value = res.access_token
    localStorage.setItem('token', res.access_token)
    await fetchMe()
    router.push('/tasks')
  }

  async function register(username: string, password: string) {
    await authApi.register({ username, password })
    await login(username, password)
  }

  async function fetchMe() {
    try {
      user.value = await authApi.me()
    } catch (e) {
      console.warn('[AuthStore] fetchMe failed, logging out')
      logout()
    }
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('token')
    router.push('/login')
  }

  // Initialize user info on store creation if token exists
  if (token.value) {
    fetchMe()
  }

  return { token, user, isLoggedIn, login, register, logout, fetchMe }
})
