import axios from 'axios'

interface DiagEntry {
  ts: string
  tag: string
  msg: string
}

class DiagLogger {
  private buffer: DiagEntry[] = []
  private flushTimer: number | null = null
  private readonly MAX_BUFFER = 50
  private readonly FLUSH_MS = 3000
  private taskId: string | null = null

  setTaskId(id: string) {
    this.taskId = id
  }

  log(tag: string, ...args: unknown[]) {
    const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')
    this.enqueue(tag, msg)
  }

  warn(tag: string, ...args: unknown[]) {
    const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')
    this.enqueue(tag, msg)
  }

  error(tag: string, ...args: unknown[]) {
    const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')
    this.enqueue(tag, msg)
  }

  private enqueue(tag: string, msg: string) {
    this.buffer.push({ ts: new Date().toISOString(), tag, msg })
    if (this.buffer.length >= this.MAX_BUFFER) {
      this.flush()
    }
  }

  private async flush() {
    if (this.buffer.length === 0) return
    const batch = [...this.buffer]
    this.buffer = []
    try {
      await axios.post('/api/logs/diag', { taskId: this.taskId, logs: batch }, { timeout: 5000 })
    } catch {
    }
  }

  startAutoFlush() {
    if (this.flushTimer) return
    this.flushTimer = window.setInterval(() => this.flush(), this.FLUSH_MS)
  }

  destroy() {
    if (this.flushTimer) {
      clearInterval(this.flushTimer)
      this.flushTimer = null
    }
    this.flush()
  }
}

export const diagLogger = new DiagLogger()
diagLogger.startAutoFlush()
