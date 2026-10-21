import axios from 'axios'
import { useAuthStore } from '@/stores/auth'

// All logs are sent to the backend; nothing is printed to the browser console.

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'
export type LogCategory =
  | 'stream'
  | 'task'
  | 'playback'
  | 'websocket'
  | 'hls'
  | 'webrtc'
  | 'ui'
  | 'api'
  | 'system'

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
      // Drop failed batches to avoid unbounded accumulation
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

  stream(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('stream', `Task ${taskId}: ${action}`, details)
  }

  playback(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('playback', `Task ${taskId}: ${action}`, details)
  }

  websocket(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('websocket', `Task ${taskId}: ${action}`, details)
  }

  hls(action: string, taskId: string, details?: Record<string, unknown>) {
    this.info('hls', `Task ${taskId}: ${action}`, details)
  }

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
