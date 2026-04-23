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
  detection_config: Record<string, any> | null
  has_history: boolean
  first_session_start_time: string | null
  session_count: number
  created_at: string
  updated_at: string
}

export interface TaskListRes {
  total: number
  items: Task[]
}

export interface DetectionRecord {
  id: string
  class_name: string
  confidence: number
  box: number[]
  detected_at: string
}

export interface TaskResult {
  type: string
  url?: string
  websocket_url?: string
  detections?: Array<{ box: number[]; confidence: number; class: string }>
  detections_count?: number
}

export interface VideoSegment {
  filename: string
  url: string
  duration: number
  first_session_start_time: string | null
  session_count: number
}

export const tasksApi = {
  list: (params?: { skip?: number; limit?: number; status?: string; search?: string }): Promise<TaskListRes> =>
    request.get('/tasks', { params }),

  get: (id: string): Promise<Task> =>
    request.get(`/tasks/${id}`),

  create: (formData: FormData): Promise<Task> =>
    request.post('/tasks', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    }),

  update: (id: string, data: { name?: string; description?: string }): Promise<Task> =>
    request.put(`/tasks/${id}`, data),

  updateDetectionConfig: (id: string, config: { detection_config: any }): Promise<{ message: string }> =>
    request.put(`/tasks/${id}/detection-config`, config),

  getDetectionRecords: (id: string, params?: { limit?: number; skip?: number; order?: string }): Promise<{ records: DetectionRecord[] }> =>
    request.get(`/tasks/${id}/detection-records`, { params }),

  delete: (id: string): Promise<{ message: string }> =>
    request.delete(`/tasks/${id}`),

  execute: (id: string): Promise<{ message: string; position: number }> =>
    request.post(`/tasks/${id}/execute`),

  pause: (id: string): Promise<{ message: string }> =>
    request.post(`/tasks/${id}/pause`),

  result: (id: string): Promise<TaskResult> =>
    request.get(`/tasks/${id}/result`),

  getHistoryVideos: (id: string, config?: { signal?: AbortSignal }): Promise<{ segments: VideoSegment[] }> =>
    request.get(`/tasks/${id}/videos`, config),

  getMergedM3u8Url: (id: string): string =>
    `${window.location.origin}/api/tasks/${id}/stream.m3u8`,

  getLiveM3u8Url: (id: string): string =>
    `${window.location.origin}/api/storage/${id}/live/index.m3u8`,

  logPlayback: (id: string, event_type: string, details: Record<string, unknown>): Promise<{ status: string }> =>
    request.post(`/tasks/${id}/playback-log`, {
      event_type,
      timestamp: Date.now() / 1000,
      details
    }),
}
