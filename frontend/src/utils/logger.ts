import axios from 'axios'
import { useAuthStore } from '@/stores/auth'

/**
 * 结构化日志系统
 * 所有日志发送到后端存储，禁止在浏览器控制台输出
 */

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'
export type LogCategory =
  | 'stream'      // 视频流操作
  | 'task'        // 任务管理
  | 'playback'    // 播放相关
  | 'websocket'   // WebSocket 连接
  | 'hls'         // HLS 播放
  | 'webrtc'      // WebRTC 播放
  | 'ui'          // UI 交互
  | 'api'         // API 调用
  | 'system'      // 系统级

interface LogEntry {
  timestamp: string
  level: LogLevel
  category: LogCategory
  message: string
  details?: Record<string, unknown>
  userAgent?: string
  url?: string
}

class Logger {
  private buffer: LogEntry[] = []
  private flushInterval: number | null = null
  private readonly BUFFER_SIZE = 20
  private readonly FLUSH_INTERVAL = 5000

  constructor() {
    this.startFlushTimer()
  }

  private startFlushTimer() {
    if (this.flushInterval) return
    this.flushInterval = window.setInterval(() => {
      this.flush()
    }, this.FLUSH_INTERVAL)
  }

  private createEntry(
    level: LogLevel,
    category: LogCategory,
    message: string,
    details?: Record<string, unknown>
  ): LogEntry {
    return {
      timestamp: new Date().toISOString(),
      level,
      category,
      message,
      details,
      userAgent: navigator.userAgent,
      url: window.location.href,
    }
  }

  private async flush() {
    if (this.buffer.length === 0) return

    const logsToSend = [...this.buffer]
    this.buffer = []

    try {
      const authStore = useAuthStore()
      const headers: Record<string, string> = {}
      if (authStore.token) {
        headers['Authorization'] = `Bearer ${authStore.token}`
      }
      await axios.post('/api/logs/batch', { logs: logsToSend }, { timeout: 5000, headers })
    } catch {
      // 如果发送失败，丢弃日志，避免累积
    }
  }

  private enqueue(level: LogLevel, category: LogCategory, message: string, details?: Record<string, unknown>) {
    const entry = this.createEntry(level, category, message, details)
    this.buffer.push(entry)

    if (this.buffer.length >= this.BUFFER_SIZE) {
      this.flush()
    }
  }

  debug(category: LogCategory, message: string, details?: Record<string, unknown>) {
    this.enqueue('debug', category, message, details)
  }

  info(category: LogCategory, message: string, details?: Record<string, unknown>) {
    this.enqueue('info', category, message, details)
  }

  warn(category: LogCategory, message: string, details?: Record<string, unknown>) {
    this.enqueue('warn', category, message, details)
  }

  error(category: LogCategory, message: string, details?: Record<string, unknown>) {
    this.enqueue('error', category, message, details)
  }

  // 流操作专用日志
  stream(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('stream', `Task ${taskId}: ${action}`, details)
  }

  // 播放专用日志
  playback(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('playback', `Task ${taskId}: ${action}`, details)
  }

  // WebSocket 专用日志
  websocket(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('websocket', `Task ${taskId}: ${action}`, details)
  }

  // HLS 专用日志
  hls(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('hls', `Task ${taskId}: ${action}`, details)
  }

  // 任务操作日志
  task(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('task', `Task ${taskId}: ${action}`, details)
  }

  destroy() {
    if (this.flushInterval) {
      clearInterval(this.flushInterval)
      this.flushInterval = null
    }
    this.flush()
  }
}

export const logger = new Logger()
