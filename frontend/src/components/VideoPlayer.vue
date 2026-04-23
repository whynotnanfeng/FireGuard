<template>
  <div class="video-player-container" role="region" aria-label="视频播放器">
    <div class="video-player">
      <div v-if="showOverlay" class="overlay">
        <template v-if="streamState === 'connecting'">
          <div class="state-icon connecting-pulse"></div>
          <div class="state-label">正在连接视频源...</div>
        </template>
        <template v-else-if="streamState === 'retry'">
          <div class="state-icon retry-spin"></div>
          <div class="state-label">正在重试连接 ({{ retryAttempt }}/{{ retryMax }})</div>
        </template>
        <template v-else-if="streamState === 'loading'">
          <div class="state-icon loading-bar-wrap">
            <div class="loading-bar"></div>
          </div>
          <div class="state-label">加载中...</div>
        </template>
        <template v-else-if="streamState === 'exception'">
          <div class="state-icon exception-icon">&#9888;</div>
          <div class="state-label">连接中断</div>
          <div class="state-sub">请检查网络或查看历史录像</div>
        </template>
      </div>

      <div class="frame-container">
        <video
          ref="videoPlayerRef"
          autoplay
          muted
          playsinline
          preload="auto"
          class="video-frame"
          :class="{ 'playback-video': playbackMode }"
          @timeupdate="onTimeUpdate"
          @ended="handleVideoEnded"
          @play="videoPaused = false"
          @pause="videoPaused = true"
          @error="onVideoError"
          @waiting="onVideoWaiting"
          @stalled="onVideoStalled"
        ></video>

        <canvas
          v-show="streamState === 'running' || playbackMode"
          ref="overlayCanvasRef"
          class="video-frame detection-canvas"
          style="position: absolute; top: 0; left: 0; pointer-events: none; z-index: 5;"
        ></canvas>

        <div class="controls" v-if="canControl">
          <div class="mode-tag" :class="{ 'is-live': !playbackMode }">
            {{ playbackMode ? '历史回放' : '实时监控' }}
          </div>

          <a-divider type="vertical" border-color="rgba(255,255,255,0.2)" />

          <div class="controls-action-group">
            <a-tooltip :title="isActuallyPlaying ? '暂停' : '播放'">
              <a-button
                type="primary"
                shape="circle"
                @click="handleUnifiedPlayPause"
                class="play-pause-btn"
              >
                <template #icon>
                  <PauseOutlined v-if="isActuallyPlaying" />
                  <CaretRightOutlined v-else />
                </template>
              </a-button>
            </a-tooltip>

            <a-tooltip title="返回实时监控">
              <a-button
                v-show="playbackMode"
                shape="circle"
                @click="jumpToLive"
                class="rollback-btn"
                style="margin-left: 12px;"
              >
                <template #icon><RollbackOutlined /></template>
              </a-button>
            </a-tooltip>
          </div>
        </div>
      </div>

      <div v-if="shouldShowTimeline" class="history-seekbar-container">
        <div class="seekbar-label" style="color: #eee; font-weight: 500; font-variant-numeric: tabular-nums;">
          {{ formatDuration(currentGlobalTime) }} / {{ formatDuration(totalDuration) }}
        </div>
        <div
          class="seekbar-wrap"
          ref="seekbarWrapRef"
          @mousemove="handleSeekBarHover"
          @mouseleave="showHoverTooltip = false"
          style="position: relative;"
        >
          <div v-if="showHoverTooltip" class="seekbar-tooltip" :style="{ left: hoverX + 'px' }">
            {{ hoverTimeAbs }}
          </div>
          <a-slider
            v-model:value="currentGlobalTime"
            :max="totalDuration"
            :tip-formatter="formatDuration"
            @change="handleGlobalSeek"
            @afterChange="isUserSeeking = false"
          />
        </div>
      </div>
    </div>

    <div class="detection-records-panel">
      <div class="panel-header">
        <h3>检测记录</h3>
        <a-button size="small" @click="refreshRecords" :loading="loadingRecords">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </div>

      <div class="records-list" v-if="(records?.length || 0) > 0">
        <div v-for="record in paginatedRecords" :key="record.id" class="record-item">
          <div class="record-time">{{ formatTime(record.detected_at) }}</div>
          <div class="record-details">
            <span class="record-class" :class="{ 'is-fire': record.class_name === 'fire' }">
              {{ record.class_name }}
            </span>
            <span class="record-confidence">{{ record.confidence.toFixed(3) }}</span>
          </div>
        </div>
      </div>

      <div v-else class="empty-records">
        <a-empty description="暂无检测记录" :image="Empty.PRESENTED_IMAGE_SIMPLE" />
      </div>

      <div class="records-pagination" v-if="(records?.length || 0) > pageSize">
        <a-pagination
          v-model:current="currentPage"
          :total="records?.length || 0"
          :page-size="pageSize"
          size="small"
          :show-size-changer="false"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import Hls from 'hls.js'
import { useTaskStore } from '@/stores/task'
import { tasksApi, type DetectionRecord } from '@/api/tasks'
import axios from 'axios'
import { terminatingTasks } from '@/api/request'
import {
  PauseOutlined,
  CaretRightOutlined,
  CloseOutlined,
  ReloadOutlined,
  HistoryOutlined,
  RollbackOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { Empty, message } from 'ant-design-vue'

type StreamState = 'connecting' | 'retry' | 'loading' | 'running' | 'exception' | 'recovering' | 'paused_end'

const props = defineProps<{
  taskId: string
  wsUrl: string
}>()

const emit = defineEmits(['close', 'status-change'])

const videoPlayerRef = ref<HTMLVideoElement | null>(null)
const overlayCanvasRef = ref<HTMLCanvasElement | null>(null)
const streamState = ref<StreamState>('connecting')
const isFrozen = ref(false)
const errorMsg = ref('')
const retryAttempt = ref(0)
const retryMax = ref(5)
const resuming = ref(false)

const taskStore = useTaskStore()

let ws: WebSocket | null = null
let isActive = true
let historyController: AbortController | null = null
let rafId: number | null = null
let connectionTimeout: any = null

const playbackMode = ref(false)
let hlsInstance: Hls | null = null
let liveHlsInstance: Hls | null = null
const liveVideoReady = ref(false)

const currentGlobalTime = ref(0)
const totalDuration = ref(0)
const isUserSeeking = ref(false)
let pendingSeekTime: number | null = null

const seekbarWrapRef = ref<HTMLElement | null>(null)
const showHoverTooltip = ref(false)
const hoverX = ref(0)
const hoverTimeAbs = ref('')
const taskRealStartTime = ref<number | null>(null)

const records = ref<DetectionRecord[]>([])
const loadingRecords = ref(false)
const loadingHistory = ref(false)
const currentPage = ref(1)
const pageSize = 20
let recordsInterval: number | null = null
let historyInterval: number | null = null
let liveProgressTimer: number | null = null

const currentDetectionConfig = ref<any>(null)

const wsReconnectCount = ref(0)
const WS_MAX_RECONNECT = 5
let resizeObserver: ResizeObserver | null = null

const sendDebugLog = (event_type: string, details: any = {}) => {
  if (!props.taskId) return
  tasksApi.logPlayback(props.taskId, event_type, {
    playbackMode: playbackMode.value,
    streamState: streamState.value,
    currentTime: videoPlayerRef.value?.currentTime,
    duration: videoPlayerRef.value?.duration,
    paused: videoPlayerRef.value?.paused,
    totalDuration: totalDuration.value,
    currentGlobalTime: currentGlobalTime.value,
    isUserSeeking: isUserSeeking.value,
    ...details
  }).catch(() => {})
}

function onVideoError(e: any) {
  const v = e.target as HTMLVideoElement
  sendDebugLog('VIDEO_ERROR', {
    errorCode: v.error?.code,
    errorMsg: v.error?.message,
    networkState: v.networkState,
    readyState: v.readyState
  })
}

function onVideoWaiting() {
  sendDebugLog('VIDEO_WAITING', { msg: 'Buffer empty, waiting...' })
}

function onVideoStalled() {
  sendDebugLog('VIDEO_STALLED', { msg: 'Network stalled, download stopped' })
}

const lastDetections = ref<any[]>([])

const syncDisplaySize = () => {
  const media = videoPlayerRef.value
  const canvas = overlayCanvasRef.value

  if (canvas && media) {
    if (media.clientWidth === 0 || media.clientHeight === 0) return

    if (canvas.width !== media.clientWidth || canvas.height !== media.clientHeight) {
      canvas.width = media.clientWidth
      canvas.height = media.clientHeight
    }
  }
}

const drawDetections = (detections: any[]) => {
  if (!isActive) return
  const canvas = overlayCanvasRef.value
  const media = videoPlayerRef.value

  if (!canvas || !media) return

  const naturalW = media.videoWidth
  const naturalH = media.videoHeight

  if (!naturalW || !naturalH) return

  const ctx = canvas.getContext('2d')
  if (!ctx) return

  if (canvas.width !== media.clientWidth || canvas.height !== media.clientHeight) {
    canvas.width = media.clientWidth
    canvas.height = media.clientHeight
  }
  if (canvas.width === 0 || canvas.height === 0) return

  ctx.clearRect(0, 0, canvas.width, canvas.height)
  if (!detections || detections.length === 0) return

  const imgAspect = naturalW / naturalH
  const canvasAspect = canvas.width / canvas.height

  let renderW: number, renderH: number, offsetX: number, offsetY: number

  if (imgAspect > canvasAspect) {
    renderW = canvas.width
    renderH = canvas.width / imgAspect
    offsetX = 0
    offsetY = (canvas.height - renderH) / 2
  } else {
    renderH = canvas.height
    renderW = canvas.height * imgAspect
    offsetX = (canvas.width - renderW) / 2
    offsetY = 0
  }

  const scaleX = renderW / naturalW
  const scaleY = renderH / naturalH

  detections.forEach(det => {
    let [x1, y1, x2, y2] = det.box

    x1 = offsetX + x1 * scaleX
    y1 = offsetY + y1 * scaleY
    x2 = offsetX + x2 * scaleX
    y2 = offsetY + y2 * scaleY

    const w = x2 - x1
    const h = y2 - y1

    ctx.strokeStyle = '#ff4d4f'
    ctx.lineWidth = 2
    ctx.strokeRect(x1, y1, w, h)

    const clsName = det.class_name || det.class || det.label || 'unknown'
    const label = `${clsName} ${Math.round(det.confidence * 100)}%`
    ctx.font = '12px Inter, sans-serif'
    const labelWidth = ctx.measureText(label).width
    ctx.fillStyle = 'rgba(255, 77, 79, 0.85)'
    ctx.fillRect(x1, Math.max(0, y1 - 20), labelWidth + 10, 20)

    ctx.fillStyle = 'white'
    ctx.fillText(label, x1 + 5, Math.max(14, y1 - 6))
  })
}

const clearCanvas = () => {
  const canvas = overlayCanvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height)
}

const showOverlay = computed(() => {
  if (playbackMode.value) return false
  if (liveVideoReady.value) return false
  return streamState.value !== 'running'
})

const shouldShowTimeline = computed(() =>
  ['running', 'connecting', 'retry', 'loading', 'exception', 'paused_end'].includes(streamState.value)
)

const canControl = computed(() => {
  return streamState.value !== 'exception' || playbackMode.value
})

const videoPaused = ref(false)

const isActuallyPlaying = computed(() => {
  if (!playbackMode.value) {
    return !isFrozen.value
  } else {
    return !videoPaused.value
  }
})

const paginatedRecords = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return records.value.slice(start, start + pageSize)
})

onMounted(async () => {
  isActive = true
  fetchTaskStartTime()
  sendDebugLog('COMPONENT_MOUNTED')

  try {
    const task = await tasksApi.get(props.taskId)
    if (task && task.detection_config) {
      currentDetectionConfig.value = typeof task.detection_config === 'string'
        ? JSON.parse(task.detection_config)
        : task.detection_config
    }

    if (task && (task.status === 'pending' || task.status === 'paused')) {
      playbackMode.value = true
      streamState.value = 'paused_end'

      tasksApi.getDetectionRecords(props.taskId, { limit: 1000 }).then(res => {
        records.value = res.records || []
      }).catch(err => {
        console.warn("[History API] fetch failed:", err)
      })

      await loadHistory()
      initVodHls()
    } else {
      connect()
      startHistorySync()
      initLiveHls()
      startLiveProgressTimer()
    }
  } catch (e) {
    console.error('Failed to load initial config', e)
    connect()
    startHistorySync()
    initLiveHls()
    startLiveProgressTimer()
  }

  if (videoPlayerRef.value) {
    resizeObserver = new ResizeObserver(() => {
      syncDisplaySize()
    })
    resizeObserver.observe(videoPlayerRef.value)
  }
})

onUnmounted(() => {
  try {
    isActive = false

    stopHistorySync()
    stopLiveProgressTimer()

    if (resizeObserver) {
      resizeObserver.disconnect()
      resizeObserver = null
    }

    if (connectionTimeout) {
      clearTimeout(connectionTimeout)
      connectionTimeout = null
    }

    forceDisconnect()

    destroyAllHls()

    if (videoPlayerRef.value) {
      videoPlayerRef.value.pause()
      videoPlayerRef.value.src = ""
      videoPlayerRef.value.load()
    }

    if (recordsInterval) {
      clearInterval(recordsInterval)
      recordsInterval = null
    }
  } catch (e) {
    console.error('[Critical] Unmount failure:', e)
  }
})

watch(playbackMode, (isPlayback) => {
  if (isPlayback) {
    stopLiveProgressTimer()
    tasksApi.getDetectionRecords(props.taskId, { limit: 1000 }).then(res => {
      records.value = res.records || []
    }).catch(e => console.warn(e))

    loadHistory()
    destroyLiveHls()
    liveVideoReady.value = false
    initVodHls()
  } else {
    destroyVodHls()
    initLiveHls()
    startLiveProgressTimer()
  }
})

watch(() => props.taskId, (newId) => {
  if (newId) {
    forceDisconnect()
    stopHistorySync()
    stopLiveProgressTimer()
    isFrozen.value = false
    liveVideoReady.value = false
    playbackMode.value = false
    destroyAllHls()
    connect()
    loadHistory()
    startHistorySync()
    initLiveHls()
    startLiveProgressTimer()
  }
})

const currentTask = computed(() => taskStore.tasks.find(t => t.id === props.taskId))
watch(() => currentTask.value?.status, (status) => {
  if (status === 'running' && (streamState.value === 'paused_end' || streamState.value === 'exception')) {
    console.log('[Auto-Sync] Task is running again. Reconnecting...')
    connect()
    if (!liveHlsInstance) {
      initLiveHls()
      startLiveProgressTimer()
    }
  }
})

function connect() {
  const token = localStorage.getItem('token')
  isActive = true

  streamState.value = 'connecting'
  errorMsg.value = ''
  retryAttempt.value = 0
  wsReconnectCount.value = 0

  // If we stay in 'connecting' for too long (e.g. 15s), show an error instead of hanging with a gray mask
  if (connectionTimeout) clearTimeout(connectionTimeout)
  connectionTimeout = setTimeout(() => {
    if (streamState.value === 'connecting' && isActive) {
      console.warn('[Connection Guard] Initial connection timed out.')
      streamState.value = 'exception'
      errorMsg.value = '连接推理引擎超时，请尝试刷新页面或重新执行任务'
    }
  }, 15000)

  ws = new WebSocket(`${props.wsUrl}/${props.taskId}?token=${token}`)

  ws.onopen = () => {
    syncDisplaySize()
  }

  ws.onmessage = (event) => {
    clearTimeout(connectionTimeout)
    if (!isActive) {
      ;(event.currentTarget as WebSocket)?.close()
      return
    }

    try {
      const data = JSON.parse(event.data)

      if (data.type === 'status') {
        if (data.status === 'deleted') {
          terminatingTasks.add(props.taskId)
          message.warning('当前任务已被移除')
          emit('close')
          return
        }
      }

      switch (data.type) {
        case 'snapshot':
          records.value = data.records || []
          break

        case 'record_event':
          if (data.record && !playbackMode.value) {
            records.value = [data.record, ...records.value].slice(0, 1000)
          }
          break

        case 'action':
          if (data.action === "stop") {
            stop()
          } else if (data.action === "update_config") {
            currentDetectionConfig.value = data.config
          }
          break

        case 'connecting':
          if (streamState.value !== 'running') {
            streamState.value = 'connecting'
          }
          break

        case 'loading':
          if (streamState.value !== 'running') {
            streamState.value = 'loading'
          }
          break

        case 'running':
          streamState.value = 'running'
          errorMsg.value = ''
          retryAttempt.value = 0
          wsReconnectCount.value = 0
          if (!liveVideoReady.value && !playbackMode.value) {
            if (!liveHlsInstance) {
              initLiveHls()
              startLiveProgressTimer()
            } else {
              liveHlsInstance.startLoad()
            }
          }
          break

        case 'retry':
          streamState.value = 'retry'
          retryAttempt.value = data.attempt ?? 1
          retryMax.value = data.max ?? 5
          break

        case 'heartbeat':
          break

        case 'paused_with_history':
          streamState.value = 'paused_end'
          errorMsg.value = data.message || '任务已暂停，可查看历史检测记录'
          break

        case 'error':
          streamState.value = 'exception'
          errorMsg.value = data.message || '连接失败，请检查视频源'
          emit('status-change', 'exception')
          break

        case 'status':
          if (data.status === 'exception') {
            streamState.value = 'exception'
            errorMsg.value = data.message || ''
            emit('status-change', 'exception')
          }
          break
      }
    } catch (e) {
      console.error('[WS] Failed to parse message:', e)
    }
  }

  ws.onclose = (event) => {
    if (connectionTimeout) {
      clearTimeout(connectionTimeout)
      connectionTimeout = null
    }
    if (!isActive) return

    if (streamState.value === 'exception' ||
        streamState.value === 'paused_end') {
      return
    }

    if (wsReconnectCount.value < WS_MAX_RECONNECT) {
      wsReconnectCount.value++
      streamState.value = 'recovering'
      const delay = wsReconnectCount.value * 1000

      setTimeout(() => {
        if (isActive) {
          connect()
        }
      }, delay)
    } else {
      tasksApi.get(props.taskId).then(task => {
        if (task.status === 'running') {
          streamState.value = 'connecting'
          errorMsg.value = '画面同步不稳定，正在尝试后台恢复...'
          setTimeout(() => {
            if (isActive) connect()
          }, 5000)
        } else {
          streamState.value = 'exception'
          errorMsg.value = '视频流连接已断开，且任务已停止'
          emit('status-change', 'exception')
        }
      }).catch(() => {
        streamState.value = 'exception'
        errorMsg.value = '视频流连接已断开，且无法同步后台状态'
        emit('status-change', 'exception')
      })
    }
  }

  ws.onerror = (event) => {
    console.error('[WS] Error:', event)
    loadHistory()
  }
}

function initLiveHls() {
  if (!videoPlayerRef.value) return

  destroyLiveHls()

  const liveUrl = `${tasksApi.getLiveM3u8Url(props.taskId)}?_=${Date.now()}`

  if (Hls.isSupported()) {
    liveHlsInstance = new Hls({
      enableWorker: true,
      lowLatencyMode: true,
      backBufferLength: 90,
      liveBackBufferLength: 90,
      liveSyncDurationCount: 1,
      liveMaxLatencyDurationCount: 3,
      liveDurationInfinity: true,
      maxBufferLength: 30,
      maxMaxBufferLength: 600,
      maxBufferHole: 0.5,
      highBufferWatchdogPeriod: 2,
    })

    liveHlsInstance.loadSource(liveUrl)
    liveHlsInstance.attachMedia(videoPlayerRef.value)

    liveHlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
      sendDebugLog('LIVE_HLS_READY')
      liveVideoReady.value = true
      if (videoPlayerRef.value) {
        videoPlayerRef.value.muted = true
        videoPlayerRef.value.play().catch(() => {})
      }
    })

    liveHlsInstance.on(Hls.Events.ERROR, (event, data) => {
      sendDebugLog('LIVE_HLS_ERROR', {
        errorType: data.type,
        errorDetail: data.details,
        fatal: data.fatal,
      })
      if (data.fatal) {
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            setTimeout(() => {
              if (liveHlsInstance && !playbackMode.value) {
                liveHlsInstance.startLoad()
              }
            }, 2000)
            break
          case Hls.ErrorTypes.MEDIA_ERROR:
            liveHlsInstance?.recoverMediaError()
            break
          default:
            destroyLiveHls()
            break
        }
      }
    })
  } else if (videoPlayerRef.value.canPlayType('application/vnd.apple.mpegurl')) {
    videoPlayerRef.value.src = liveUrl
    liveVideoReady.value = true
  }
}

function initVodHls() {
  if (!videoPlayerRef.value) return

  destroyVodHls()

  const vodUrl = `${tasksApi.getMergedM3u8Url(props.taskId)}?mode=vod&_=${Date.now()}`

  if (Hls.isSupported()) {
    hlsInstance = new Hls({
      enableWorker: true,
      backBufferLength: 90,
    })

    hlsInstance.loadSource(vodUrl)
    hlsInstance.attachMedia(videoPlayerRef.value)

    hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
      sendDebugLog('VOD_HLS_READY')
      if (videoPlayerRef.value) {
        if (pendingSeekTime !== null) {
          videoPlayerRef.value.currentTime = pendingSeekTime
          pendingSeekTime = null
        } else if (!isUserSeeking.value) {
          if (currentTask.value?.status === 'running') {
            hlsInstance?.on(Hls.Events.LEVEL_LOADED, () => {
              if (videoPlayerRef.value && pendingSeekTime === null && !isUserSeeking.value) {
                const d = videoPlayerRef.value.duration
                if (isFinite(d) && d > 0) {
                  videoPlayerRef.value.currentTime = Math.max(0, d - 0.1)
                }
              }
            }, { once: true })
          }
        }

        videoPlayerRef.value.play().catch(() => {})
      }
    })

    hlsInstance.on(Hls.Events.ERROR, (event, data) => {
      if (data.fatal) {
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            hlsInstance?.startLoad()
            break
          case Hls.ErrorTypes.MEDIA_ERROR:
            hlsInstance?.recoverMediaError()
            break
          default:
            destroyVodHls()
            break
        }
      }
    })
  } else if (videoPlayerRef.value.canPlayType('application/vnd.apple.mpegurl')) {
    videoPlayerRef.value.src = vodUrl
  }
}

function destroyLiveHls() {
  if (liveHlsInstance) {
    liveHlsInstance.destroy()
    liveHlsInstance = null
  }
  liveVideoReady.value = false
}

function destroyVodHls() {
  if (hlsInstance) {
    hlsInstance.destroy()
    hlsInstance = null
  }
}

function destroyAllHls() {
  destroyLiveHls()
  destroyVodHls()
}

async function refreshRecords() {
  loadingRecords.value = true
  try {
    const res = await tasksApi.getDetectionRecords(props.taskId, { limit: 100 })
    records.value = res.records
  } catch (e) {
    console.error('Failed to refresh records', e)
  } finally {
    loadingRecords.value = false
  }
}

async function loadHistory() {
  if (loadingHistory.value) return
  if (historyController) historyController.abort()
  historyController = new AbortController()
  loadingHistory.value = true

  try {
    const res = await tasksApi.getHistoryVideos(props.taskId, { signal: historyController.signal })
    if (res.segments && res.segments.length > 0) {
      const merged = res.segments[0]
      const estimatedDuration = merged.duration || 0
      if (estimatedDuration > totalDuration.value) {
        totalDuration.value = estimatedDuration
      }

      if (merged.first_session_start_time && !taskRealStartTime.value) {
        taskRealStartTime.value = new Date(merged.first_session_start_time).getTime()
      }

      if (!playbackMode.value && records.value.length === 0) {
        try {
          const recordsRes = await tasksApi.getDetectionRecords(props.taskId, { limit: 1000 })
          records.value = recordsRes.records || []
        } catch (e) {
          console.warn('[Detection Records] Failed to load:', e)
        }
      }
    }
    sendDebugLog('LOAD_HISTORY_SUCCESS', { count: res.segments?.length })
  } catch (e: any) {
    if (axios.isCancel(e) || e.name === 'AbortError' || e.message === 'canceled') return
    console.error('[History] Failed to load:', e)
    sendDebugLog('LOAD_HISTORY_ERROR', { error: e.message })
  } finally {
    loadingHistory.value = false
  }
}

async function fetchTaskStartTime() {
  try {
    const task = await tasksApi.get(props.taskId)
    if (task.first_session_start_time) {
      taskRealStartTime.value = new Date(task.first_session_start_time).getTime()
      return
    }
  } catch (e) {
    console.warn("Failed to fetch task start time from task API", e)
  }

  try {
    const res = await tasksApi.getDetectionRecords(props.taskId, {
      limit: 1,
      skip: 0,
      order: 'asc'
    })
    if (res.records && res.records.length > 0) {
      taskRealStartTime.value = new Date(res.records[0].detected_at).getTime()
    }
  } catch (e) {
    console.warn("Failed to fetch task start time from records", e)
  }
}

function syncDetectionsToTime(currentTime: number) {
  if (records.value.length === 0) {
    clearCanvas()
    return
  }

  const oldestRecord = records.value[records.value.length - 1]
  const taskStartTime = new Date(oldestRecord.detected_at).getTime()
  const currentAbsTime = taskStartTime + Math.floor(currentTime * 1000)

  const currentDetections = records.value.filter(rec => {
    const recTime = new Date(rec.detected_at).getTime()
    return Math.abs(recTime - currentAbsTime) < 1500
  }).map(rec => ({
    box: rec.box,
    class_name: rec.class_name,
    confidence: rec.confidence
  }))

  syncDisplaySize()
  if (currentDetections.length > 0) {
    drawDetections(currentDetections)
  } else {
    clearCanvas()
  }
}

function onTimeUpdate(e: Event) {
  const video = e.target as HTMLVideoElement

  if (playbackMode.value) {
    if (!isNaN(video.duration) && isFinite(video.duration)) {
      totalDuration.value = video.duration
    }
    if (!isUserSeeking.value) {
      currentGlobalTime.value = video.currentTime
    }
  } else {
    if (taskRealStartTime.value) {
      const elapsed = (Date.now() - taskRealStartTime.value) / 1000
      totalDuration.value = elapsed
      if (!isUserSeeking.value) {
        currentGlobalTime.value = elapsed
      }
    }
  }

  if (!isUserSeeking.value && records.value.length > 0) {
    syncDetectionsToTime(video.currentTime)
  }
}

function handleSeekBarHover(e: MouseEvent) {
  if (!seekbarWrapRef.value || totalDuration.value <= 0) return

  const rect = seekbarWrapRef.value.getBoundingClientRect()
  const x = e.clientX - rect.left
  const percent = Math.max(0, Math.min(1, x / rect.width))
  const hoverSeconds = percent * totalDuration.value

  hoverX.value = x
  showHoverTooltip.value = true

  if (taskRealStartTime.value) {
    const absTime = new Date(taskRealStartTime.value + hoverSeconds * 1000)
    hoverTimeAbs.value = absTime.toLocaleTimeString('zh-CN', {
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    })
  } else {
    hoverTimeAbs.value = formatDuration(hoverSeconds)
  }
}

watch(records, () => {
  if (videoPlayerRef.value && !isNaN(videoPlayerRef.value.currentTime)) {
    syncDetectionsToTime(videoPlayerRef.value.currentTime)
  }
}, { deep: true })

async function handleGlobalSeek(targetSeconds: number) {
  isUserSeeking.value = true
  currentGlobalTime.value = targetSeconds
  sendDebugLog('SEEK_CHANGE', { targetTime: targetSeconds })

  syncDetectionsToTime(targetSeconds)

  if (!playbackMode.value) {
    pendingSeekTime = targetSeconds
    playbackMode.value = true
  } else {
    const v = videoPlayerRef.value
    if (v) {
      v.currentTime = targetSeconds
      if (v.paused) v.play().catch(() => {})
    }
  }
}

function handleUnifiedPlayPause() {
  if (!playbackMode.value) {
    toggleFreeze()
  } else if (videoPlayerRef.value) {
    if (videoPlayerRef.value.paused) {
      if (videoPlayerRef.value.currentTime >= videoPlayerRef.value.duration - 0.5) {
        videoPlayerRef.value.currentTime = 0
      }
      videoPlayerRef.value.play().catch(e => {
        console.error("Playback failed:", e)
        message.error("播放失败")
      })
      videoPaused.value = false
    } else {
      videoPlayerRef.value.pause()
      videoPaused.value = true
    }
  }
}

function jumpToLive() {
  const task = currentTask.value
  if (task && (task.status === 'pending' || task.status === 'paused')) {
    message.warning("请先执行任务连接视频源！")
    return
  }

  playbackMode.value = false
  isFrozen.value = false
  sendDebugLog('JUMP_TO_LIVE')

  clearCanvas()
  initLiveHls()
  startLiveProgressTimer()
  connect()
}

function startHistorySync() {
  stopHistorySync()

  const syncFunc = () => {
    if (!isActive) return
    if (!isUserSeeking.value) {
      loadHistory()
    }

    if (!playbackMode.value) {
      tasksApi.getDetectionRecords(props.taskId, { limit: 1000 })
        .then(res => {
          if (res.records && res.records.length > records.value.length) {
            records.value = res.records
          }
        })
        .catch(() => {})
    }

    const nextInterval = (!playbackMode.value) ? 1500 : 10000
    historyInterval = window.setTimeout(syncFunc, nextInterval)
  }

  historyInterval = window.setTimeout(syncFunc, 500)
}

function startLiveProgressTimer() {
  stopLiveProgressTimer()

  const tick = () => {
    if (!isActive || playbackMode.value) return
    if (taskRealStartTime.value && !isUserSeeking.value) {
      const elapsed = (Date.now() - taskRealStartTime.value) / 1000
      totalDuration.value = elapsed
      currentGlobalTime.value = elapsed
    }
    liveProgressTimer = window.setTimeout(tick, 500)
  }

  liveProgressTimer = window.setTimeout(tick, 200)
}

function stopLiveProgressTimer() {
  if (liveProgressTimer) {
    clearTimeout(liveProgressTimer)
    liveProgressTimer = null
  }
}

function handleVideoEnded() {
  sendDebugLog('VIDEO_ENDED')
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const parts = [m, s].map(v => v.toString().padStart(2, '0'))
  if (h > 0) parts.unshift(h.toString())
  return parts.join(':')
}

function forceDisconnect() {
  isActive = false
  wsReconnectCount.value = 0
  if (ws) {
    ws.onopen = null
    ws.onmessage = null
    ws.onclose = null
    ws.onerror = null
    try {
      ws.close()
    } catch(e) {}
    ws = null
  }
}

function toggleFreeze() {
  isFrozen.value = !isFrozen.value
}

async function handleResumeTask() {
  resuming.value = true
  try {
    await tasksApi.execute(props.taskId)
    connect()
    initLiveHls()
    startLiveProgressTimer()
    message.success('任务已重新启动')
  } catch (e) {
    message.error('启动失败')
  } finally {
    resuming.value = false
  }
}

function stop() {
  forceDisconnect()
  emit('close')
}

function stopHistorySync() {
  if (historyController) {
    historyController.abort()
    historyController = null
  }
  if (historyInterval) {
    clearTimeout(historyInterval)
    historyInterval = null
  }
}

function formatTime(isoString: string): string {
  const date = new Date(isoString)
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}
</script>

<style scoped>
.video-player-container {
  display: flex;
  gap: 16px;
  height: 75vh;
  min-height: 600px;
  overflow: hidden;
}

.video-player {
  flex: 1;
  min-width: 0;
  background: var(--bg-secondary, #1a1a2e);
  border-radius: 8px;
  overflow: hidden;
  position: relative;
  border: 1px solid var(--border-color, #333);
  box-shadow: inset 0 0 40px rgba(0, 0, 0, 0.15);
}

.frame-container {
  position: relative;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.video-frame {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.controls {
  position: absolute;
  bottom: 80px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  background: rgba(0, 0, 0, 0.7);
  padding: 8px 24px;
  border-radius: 40px;
  backdrop-filter: blur(8px);
  opacity: 0;
  transition: opacity 0.3s, transform 0.3s;
  z-index: 40;
  border: 1px solid rgba(255, 255, 255, 0.15);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
}

.frame-container:hover .controls {
  opacity: 1;
  transform: translateX(-50%) translateY(-5px);
}

.overlay {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: rgba(10, 10, 20, 0.85);
  backdrop-filter: blur(2px);
}

.state-label {
  font-size: 16px;
  font-weight: 600;
  color: #e0e0e0;
}

.state-sub {
  font-size: 13px;
  color: #666;
}

.connecting-pulse {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: rgba(64, 158, 255, 0.3);
  border: 2px solid #409eff;
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.2); opacity: 0.6; }
}

.retry-spin {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  border: 3px solid transparent;
  border-top-color: #fa8c16;
  border-right-color: #fa8c16;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.loading-bar-wrap {
  width: 120px;
  height: 4px;
  background: #222;
  border-radius: 2px;
  overflow: hidden;
}

.loading-bar {
  height: 100%;
  background: linear-gradient(90deg, #409eff, #67c23a);
  border-radius: 2px;
  animation: loading-slide 1.4s ease-in-out infinite;
}

@keyframes loading-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(200%); }
}

.history-seekbar-container {
  padding: 12px 20px;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(10px);
  display: flex;
  align-items: center;
  gap: 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 30;
}

.seekbar-label {
  font-size: 12px;
  color: #888;
  white-space: nowrap;
}

.seekbar-wrap {
  flex: 1;
  position: relative;
}

.seekbar-tooltip {
  position: absolute;
  bottom: 40px;
  transform: translateX(-50%);
  background: rgba(0, 0, 0, 0.85);
  color: #fff;
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 12px;
  white-space: nowrap;
  pointer-events: none;
  z-index: 100;
  border: 1px solid rgba(255, 255, 255, 0.2);
  box-shadow: 0 4px 15px rgba(0, 0, 0, 0.5);
  font-variant-numeric: tabular-nums;
  animation: tooltipFadeIn 0.2s ease-out;
}

.seekbar-tooltip::after {
  content: '';
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: rgba(0, 0, 0, 0.85);
}

@keyframes tooltipFadeIn {
  from { opacity: 0; transform: translateX(-50%) translateY(5px); }
  to { opacity: 1; transform: translateX(-50%) translateY(0); }
}

.mode-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: #333;
  color: #aaa;
}

.mode-tag.is-live {
  background: rgba(255, 77, 79, 0.15);
  color: #ff4d4f;
  animation: blink 2s infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

.playback-video {
  background: #000;
}

.rollback-btn {
  background: rgba(255, 255, 255, 0.15);
  color: #fff;
  border: none;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
}

.rollback-btn:hover {
  background: rgba(255, 255, 255, 0.25);
  color: #409eff;
  transform: scale(1.05);
}

.detection-records-panel {
  width: 280px;
  flex-shrink: 0;
  background: var(--bg-secondary, #1a1a2e);
  border-radius: 8px;
  border: 1px solid var(--border-color, #333);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  height: 100%;
}

.panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color, #333);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
}

.panel-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary, #e0e0e0);
}

.records-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  min-height: 0;
}

.record-item {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-color, #333);
  transition: background-color 0.2s;
}

.record-item:hover {
  background-color: var(--bg-hover, rgba(255,255,255,0.05));
}

.record-time {
  font-size: 12px;
  color: var(--text-secondary, #888);
  margin-bottom: 4px;
}

.record-details {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.record-class {
  font-weight: 500;
  color: var(--text-primary, #e0e0e0);
}

.record-class.is-fire {
  color: #ff4d4f;
}

.record-confidence {
  font-size: 14px;
  font-weight: 600;
  color: var(--primary-color, #409eff);
}

.empty-records {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.records-pagination {
  padding: 8px 16px;
  border-top: 1px solid var(--border-color, #333);
  display: flex;
  justify-content: center;
  flex-shrink: 0;
}

.history-seekbar-container :deep(.ant-slider-rail) {
  background-color: rgba(255, 255, 255, 0.4) !important;
}

.history-seekbar-container :deep(.ant-slider-track) {
  background-color: var(--primary-color, #409eff) !important;
}
</style>
