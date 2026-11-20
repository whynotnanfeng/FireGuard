/**
 * 检测框缓冲池：实时/历史模式的自适应清理策略。
 *
 * 组件只负责往 buffer 里写数据与渲染，清理阈值集中在这里，
 * 避免模式参数和清理分支继续膨胀 VideoPlayer。
 */
import { ref, type Ref } from 'vue'
import { diagLogger } from '@/utils/diagLogger'

export interface DetectionBufferEntry {
  timestamp: number
  timestamp_ms?: number
  wall_clock?: number
  boxes: any[]
  is_history?: boolean
}

const CACHE_TIME_WINDOW_LIVE_MS = 15000
const CACHE_TIME_WINDOW_HISTORY_MS = 60000
const CACHE_MAX_CAPACITY_LIVE = 500
const CACHE_MAX_CAPACITY_HISTORY = 1200
const CACHE_HARD_LIMIT_LIVE = 800
const CACHE_HARD_LIMIT_HISTORY = 1500
const CACHE_EARLY_CLEANUP_THRESHOLD_LIVE = 400
const CACHE_EARLY_CLEANUP_THRESHOLD_HISTORY = 960

export function useDetectionBuffer(
  playMode: Ref<'live' | 'history'>,
  getClockOffset: () => number = () => 0,
) {
  const detectionBuffer = ref<DetectionBufferEntry[]>([])
  const lastValidRecord = ref<{ timestamp: number; boxes: any[] } | null>(null)
  let pruneLogCounter = 0

  function pruneDetectionBuffer() {
    const isHistoryMode = playMode.value === 'history'

    const timeWindowMs = isHistoryMode ? CACHE_TIME_WINDOW_HISTORY_MS : CACHE_TIME_WINDOW_LIVE_MS
    const maxCapacity = isHistoryMode ? CACHE_MAX_CAPACITY_HISTORY : CACHE_MAX_CAPACITY_LIVE
    const hardLimit = isHistoryMode ? CACHE_HARD_LIMIT_HISTORY : CACHE_HARD_LIMIT_LIVE
    const earlyCleanupThreshold = isHistoryMode
      ? CACHE_EARLY_CLEANUP_THRESHOLD_HISTORY
      : CACHE_EARLY_CLEANUP_THRESHOLD_LIVE

    if (detectionBuffer.value.length > hardLimit) {
      const beforeSize = detectionBuffer.value.length
      detectionBuffer.value = detectionBuffer.value.slice(-hardLimit)
      pruneLogCounter++
      if (pruneLogCounter % 20 === 0) {
        diagLogger.log('DIAG-PRUNE-HARD', JSON.stringify({
          event: 'hard_limit_cleanup',
          mode: isHistoryMode ? 'history' : 'live',
          beforeSize,
          afterSize: detectionBuffer.value.length,
          hardLimit,
        }))
      }
    }

    if (detectionBuffer.value.length > 2) {
      const newestTs = detectionBuffer.value[detectionBuffer.value.length - 1]?.timestamp || 0
      const timeCutoff = newestTs - timeWindowMs

      let effectiveCutoff = timeCutoff
      if (detectionBuffer.value.length > earlyCleanupThreshold) {
        effectiveCutoff = newestTs - (timeWindowMs / 2)
        pruneLogCounter++
        if (pruneLogCounter % 30 === 0) {
          diagLogger.log('DIAG-PRUNE-EARLY', JSON.stringify({
            event: 'early_cleanup',
            mode: isHistoryMode ? 'history' : 'live',
            bufferSize: detectionBuffer.value.length,
            threshold: earlyCleanupThreshold,
            timeWindowMs: timeWindowMs / 2,
          }))
        }
      }

      pruneLogCounter++
      if (pruneLogCounter % 100 === 0) {
        diagLogger.log('DIAG-PRUNE', JSON.stringify({
          mode: isHistoryMode ? 'history' : 'live',
          newestTs,
          timeCutoff,
          effectiveCutoff,
          bufferSize: detectionBuffer.value.length,
          firstTs: detectionBuffer.value[0]?.timestamp,
          clockOffset: getClockOffset(),
        }))
      }

      while (detectionBuffer.value.length > 2) {
        const first = detectionBuffer.value[0]
        if (first.timestamp < effectiveCutoff) {
          if (!isHistoryMode && !first.is_history) {
            detectionBuffer.value.shift()
          } else if (isHistoryMode) {
            detectionBuffer.value.shift()
          } else {
            break
          }
        } else {
          break
        }
      }

      if (detectionBuffer.value.length > maxCapacity) {
        const excess = detectionBuffer.value.length - maxCapacity
        detectionBuffer.value = detectionBuffer.value.slice(excess)
        if (pruneLogCounter % 30 === 0) {
          diagLogger.log('DIAG-PRUNE-CAPACITY', JSON.stringify({
            event: 'capacity_cleanup',
            mode: isHistoryMode ? 'history' : 'live',
            beforeSize: detectionBuffer.value.length + excess,
            afterSize: detectionBuffer.value.length,
            removed: excess,
            maxCapacity,
          }))
        }
      }
    }
  }

  return { detectionBuffer, lastValidRecord, pruneDetectionBuffer }
}
