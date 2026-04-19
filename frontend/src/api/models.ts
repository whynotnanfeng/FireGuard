import request from './request'

export interface DetectionModel {
  id: string
  name: string
  format: string
  input_types: string[]
  description: string
  status: string
  label_config?: Record<string, string>
  created_at: string
}

export interface ModelListRes {
  total: number
  items: DetectionModel[]
}

export const modelsApi = {
  list: (params?: { skip?: number; limit?: number }): Promise<ModelListRes> =>
    request.get('/models', { params }),

  create: (formData: FormData): Promise<DetectionModel> =>
    request.post('/models', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),

  update: (id: string, data: { name?: string; description?: string; label_config?: Record<string, string> }): Promise<DetectionModel> =>
    request.put(`/models/${id}`, data),

  delete: (id: string): Promise<{ message: string }> =>
    request.delete(`/models/${id}`),
}
