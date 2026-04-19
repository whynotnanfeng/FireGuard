import request from './request'

export interface Task {
  id: string
  name: string
  task_type: string
  input_types: string[]
  model_id: string
  model_name: string | null
  source_type: string
  source_path: string
  description: string
  status: string
  progress: number
  error_msg: string
  created_at: string
  updated_at: string
}

export interface TaskListRes {
  total: number
  items: Task[]
}

export interface TaskResult {
  type: string
  url?: string
  websocket_url?: string
  detections?: Array<{ box: number[]; confidence: number; class: string }>
  detections_count?: number
}

export const tasksApi = {
  list: (params?: { skip?: number; limit?: number; status?: string }): Promise<TaskListRes> =>
    request.get('/tasks', { params }),

  get: (id: string): Promise<Task> =>
    request.get(`/tasks/${id}`),

  create: (formData: FormData): Promise<Task> =>
    request.post('/tasks', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000, // 5 min for large uploads
    }),

  update: (id: string, data: { name?: string; description?: string }): Promise<Task> =>
    request.put(`/tasks/${id}`, data),

  delete: (id: string): Promise<{ message: string }> =>
    request.delete(`/tasks/${id}`),

  execute: (id: string): Promise<{ message: string; position: number }> =>
    request.post(`/tasks/${id}/execute`),

  pause: (id: string): Promise<{ message: string }> =>
    request.post(`/tasks/${id}/pause`),

  result: (id: string): Promise<TaskResult> =>
    request.get(`/tasks/${id}/result`),
}
