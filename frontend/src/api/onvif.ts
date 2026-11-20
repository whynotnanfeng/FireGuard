import request from './request'

export interface OnvifDevice {
  host: string
  port: number
  xaddr: string
  name: string
  hardware: string
  location: string
  uuid: string
}

export interface OnvifStreamUri {
  rtsp_url: string
  profile_token: string
  profile_name: string
  profile_count: number
  device: {
    manufacturer: string
    model: string
    firmware: string
    serial: string
  }
}

export const onvifApi = {
  discover: (timeoutMs = 3000) =>
    request.post<unknown, { devices: OnvifDevice[]; timeout_ms: number }>('/onvif/discover', {
      timeout_ms: timeoutMs
    }),
  streamUri: (payload: {
    xaddr: string
    username: string
    password: string
    profile_token?: string
  }) => request.post<unknown, OnvifStreamUri>('/onvif/stream-uri', payload)
}
