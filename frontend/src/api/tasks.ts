import request from './request'

export interface TaskModelInfo {
  model_id: string
  model_name: string | null
  weight: number
  per_class_thresholds: string | null
  enabled_classes: string | null
  order_index: number
}

export interface Task {
  id: string
  name: string
  task_type: string
  input_types: string[]
  model_id: string
  model_name: string | null
  task_models: TaskModelInfo[] | null
  source_type: string
  source_path: string
  description: string
  status: string
  progress: number
  error_msg: string
  detection_config: Record<string, any> | null
  has_history: boolean
  cumulative_running_seconds: number
  session_start_time: string | null
  first_session_start_time: string | null
  session_count: number
  resolution_width: number | null
  resolution_height: number | null
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
  timestamp_ms?: number
}

export interface DetectionEvent {
  id: string
  track_id: number
  event_type: 'enter' | 'leave' | 'update'
  class_name: string
  confidence: number
  box: number[]
  entered_at: string
  left_at: string | null
  duration_ms: number
  max_confidence: number
  avg_confidence: number
  update_count: number
}

export interface DetectionEventSummary {
  total_targets: number
  class_stats: Record<string, {
    count: number
    avg_duration_ms: number
    avg_confidence: number
  }>
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
  session_map: Array<{ index: number; start_offset_sec: number; start_abs_time_ms: number }>
}

export const tasksApi = {
  list: (params?: { skip?: number; limit?: number; status?: string; search?: string; task_type?: string; model_id?: string; description?: string; date_from?: string; date_to?: string }): Promise<TaskListRes> =>
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

  getDetectionRecords: (id: string, params?: { limit?: number; skip?: number; order?: string; start_time?: string; end_time?: string }): Promise<{ records: DetectionRecord[] }> =>
    request.get(`/tasks/${id}/detection-records`, { params }),

  getDetectionEvents: (id: string, params?: { limit?: number; skip?: number; event_type?: string; class_name?: string; order?: string; start_time?: string; end_time?: string }): Promise<{ events: DetectionEvent[] }> =>
    request.get(`/tasks/${id}/detection-events`, { params }),

  getDetectionEventsSummary: (id: string): Promise<DetectionEventSummary> =>
    request.get(`/tasks/${id}/detection-events/summary`),

  getDetections: (id: string, params: { start_time: number; end_time: number }): Promise<Array<{ timestamp: number; boxes: any[] }>> =>
    request.get(`/tasks/${id}/detections`, { params }),

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

  getSnapshot: (id: string): Promise<any> =>
    request.get(`/tasks/${id}/snapshot`),

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

  checkGpuStatus: (): Promise<{ available: boolean; checks: Record<string, any>; reason: string }> =>
    request.get('/tasks/gpu/status'),

  downloadAllResults: (id: string): Promise<Blob> =>
    request.get(`/tasks/${id}/download`, { responseType: 'blob' }),
}
