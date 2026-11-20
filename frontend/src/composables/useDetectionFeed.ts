import { ref, watch } from 'vue'
import { tasksApi, type DetectionRecord, type DetectionEvent } from '@/api/tasks'
import { logger } from '@/utils/logger'
import { diagLogger } from '@/utils/diagLogger'

export type EventFilter = 'active' | 'closed' | 'all'

/**
 * 单个播放器的检测记录/事件轮询。
 *
 * 从 VideoPlayer 中抽离，避免播放器组件同时承担 HLS、时间轴、WebSocket、
 * 记录轮询四类职责；多个播放器实例各自持有独立状态，不会串任务。
 *
 * 事件列表由服务端分页 + 状态筛选：默认只看“监测中”（enter 行），
 * 避免长寿命目标被大量短命目标挤出视野。
 */
export function useDetectionFeed(getTaskId: () => string, pollMs = 2000) {
  const records = ref<DetectionRecord[]>([])
  const loadingRecords = ref(false)
  const detectionEvents = ref<DetectionEvent[]>([])
  const activeTracks = ref(0)

  const eventFilter = ref<EventFilter>('active')
  const eventPage = ref(1)
  const eventPageSize = ref(10)
  const eventTotal = ref(0)

  let timer: number | null = null

  async function refreshRecords() {
    const taskId = getTaskId()
    if (!taskId) return
    loadingRecords.value = true
    try {
      const res = await tasksApi.getDetectionRecords(taskId, { limit: 100 })
      records.value = res.records
    } catch (e) {
      logger.error('api', 'Failed to refresh records', { taskId, error: String(e) })
    } finally {
      loadingRecords.value = false
    }
  }

  async function refreshDetectionEvents() {
    const taskId = getTaskId()
    if (!taskId) return
    const eventType = eventFilter.value === 'active'
      ? 'enter'
      : eventFilter.value === 'closed'
        ? 'leave'
        : undefined
    try {
      const [eventsRes, summaryRes] = await Promise.all([
        tasksApi.getDetectionEvents(taskId, {
          limit: eventPageSize.value,
          skip: (eventPage.value - 1) * eventPageSize.value,
          order: 'desc',
          event_type: eventType,
        }),
        tasksApi.getDetectionEventsSummary(taskId),
      ])
      detectionEvents.value = eventsRes.events
      eventTotal.value = eventsRes.total
      activeTracks.value = summaryRes.active_targets
    } catch (e) {
      diagLogger.warn('events-poll', `Failed to refresh detection events: ${e}`, { taskId })
    }
  }

  watch(eventFilter, () => {
    eventPage.value = 1
    refreshDetectionEvents()
  })
  watch([eventPage, eventPageSize], () => {
    refreshDetectionEvents()
  })

  function startEventsPolling() {
    stopEventsPolling()
    refreshDetectionEvents()
    timer = window.setInterval(refreshDetectionEvents, pollMs)
  }

  function stopEventsPolling() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  return {
    records,
    loadingRecords,
    detectionEvents,
    activeTracks,
    eventFilter,
    eventPage,
    eventPageSize,
    eventTotal,
    refreshRecords,
    refreshDetectionEvents,
    startEventsPolling,
    stopEventsPolling,
  }
}
