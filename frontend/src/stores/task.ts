import { defineStore } from 'pinia'
import { ref } from 'vue'
import { tasksApi, type Task } from '@/api/tasks'

export const useTaskStore = defineStore('task', () => {
  const tasks = ref<Task[]>([])
  const total = ref(0)
  const loading = ref(false)

  async function fetchTasks(params?: { skip?: number; limit?: number; status?: string; search?: string; task_type?: string; model_id?: string; description?: string; date_from?: string; date_to?: string }) {
    loading.value = true
    try {
      const res = await tasksApi.list(params)
      tasks.value = res.items
      total.value = res.total
    } finally {
      loading.value = false
    }
  }

  async function createTask(data: FormData) {
    const task = await tasksApi.create(data)
    tasks.value.unshift(task)
  }

  async function deleteTask(id: string) {
    await tasksApi.delete(id)
    tasks.value = tasks.value.filter((t) => t.id !== id)
    total.value--
  }

  async function checkGpuStatus() {
    return await tasksApi.checkGpuStatus()
  }

  return { tasks, total, loading, fetchTasks, createTask, deleteTask, checkGpuStatus }
})
