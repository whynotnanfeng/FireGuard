<template>
  <div class="video-player-container" role="region" aria-label="视频播放器">
    <div class="video-player">
      <div v-if="showOverlay" class="overlay">
        <template v-if="streamState === 'exception'">
          <div class="state-icon exception-icon">&#9888;</div>
          <div class="state-label">{{ errorMsg || '连接中断' }}</div>
          <div class="state-sub">{{ errorMsg ? '请根据提示检查环境配置' : '请检查网络或查看历史录像' }}</div>
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
          @playing="onVideoPlaying"
          @pause="onVideoPause"
          @waiting="onVideoWaiting"
          @error="onVideoError"
        ></video>

        <canvas
          v-if="showDetectionData"
          ref="overlayCanvasRef"
          class="video-frame detection-canvas"
          style="position: absolute; top: 0; left: 0; pointer-events: none; z-index: 5;"
        ></canvas>

        <!-- Local Video Loading Overlay -->
        <div v-if="showVideoOverlay" class="video-overlay-inner">
          <template v-if="streamState === 'connecting'">
            <div class="state-icon connecting-pulse"></div>
            <div class="state-label">正在连接视频源...</div>
          </template>
          <template v-else-if="streamState === 'retry'">
            <div class="state-icon retry-spin"></div>
            <div class="state-label">正在重试 ({{ retryAttempt }}/{{ retryMax }})</div>
          </template>
          <template v-else-if="streamState === 'loading'">
            <div class="state-icon loading-bar-wrap">
              <div class="loading-bar"></div>
            </div>
            <div class="state-label">画面加载中...</div>
          </template>
        </div>

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

            <a-tooltip :title="currentChannel === 'rgb' ? '切换到红外视角' : '切换到可见光视角'">
              <a-button
                v-show="isDualStream"
                shape="circle"
                @click="toggleChannel"
                class="rollback-btn"
                style="margin-left: 12px;"
              >
                <template #icon><SwapOutlined /></template>
              </a-button>
            </a-tooltip>
          </div>
        </div>
      </div>

      <div v-if="shouldShowTimeline" class="history-seekbar-container">
        <div class="seekbar-label" style="color: #eee; font-weight: 500; font-variant-numeric: tabular-nums;">
          {{ displayCurrentTimeStr }} / {{ displayMaxTimeStr }}
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
            :value="sliderValue"
            :max="100"
            :tip-formatter="tipFormatter"
            @change="handleGlobalSeek"
            @afterChange="isUserSeeking = false"
          />
        </div>
      </div>
    </div>

    <div class="detection-records-panel" v-if="showRecordsPanel">
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
  SwapOutlined,
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
const playMode = ref<'live' | 'history'>('live')
const frozenDuration = ref(0)
const frozenRealTime = ref(0)
let isSwitchingStream = false

let hls: Hls | null = null;
const isVideoActuallyPlaying = ref(false); 
const showDetectionData = computed(() => {
  // 必须同时满足：业务上处于 running 且 物理屏幕上已出画面
  return streamState.value === 'running' && isVideoActuallyPlaying.value;
});

const showRecordsPanel = computed(() => {
  // 侧边记录栏只要不是异常状态，就应该保持显示，实现分层解耦
  return !['exception'].includes(streamState.value);
});

const isVideoPaused = ref(false);

const showVideoOverlay = computed(() => {
  if (streamState.value === 'exception') return false;
  if (isVideoPaused.value) return false;
  
  // 核心逻辑：只要视频没在播，或者视频还没加载到足够帧（readyState < 3），且流是运行/连接中，就显示遮罩
  const isVideoReady = videoPlayerRef.value && videoPlayerRef.value.readyState >= 3;
  const loadingStates: StreamState[] = ['connecting', 'retry', 'loading', 'recovering', 'running'];
  
  return (!isVideoActuallyPlaying.value || !isVideoReady) && loadingStates.includes(streamState.value);
});


// 防抖工具函数
function debounce(fn: Function, delay: number) {
  let timer: any = null;
  return function(...args: any[]) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      fn.apply(null, args);
    }, delay);
  };
}

const sliderValue = computed(() => {
  if (playMode.value === 'live') return 100;
  const max = frozenDuration.value || totalDuration.value;
  return max > 0 ? (currentGlobalTime.value / max) * 100 : 0;
});

const displayCurrentTimeStr = computed(() => {
  if (playMode.value === 'live') {
    return formatDuration(totalDuration.value); 
  }
  return formatDuration(currentGlobalTime.value); 
});

const displayMaxTimeStr = computed(() => {
  if (playMode.value === 'live') {
    return formatDuration(totalDuration.value);
  }
  return formatDuration(frozenDuration.value || totalDuration.value); 
});

const currentGlobalAbsTime = ref<number>(0); 
const currentFragProgramDateTime = ref<number>(0);
const currentFragVideoTime = ref<number>(0);
const firstSessionStartTime = ref<number | null>(null); 

const currentGlobalTime = ref(0)
const totalDuration = ref(0)
const isUserSeeking = ref(false)
let pendingSeekTime: number | null = null

const seekbarWrapRef = ref<HTMLElement | null>(null)
const showHoverTooltip = ref(false)
const hoverX = ref(0)
const hoverTimeAbs = ref('')

// Dual-channel support
const currentChannel = ref<'rgb' | 'ir'>('rgb')
const currentTask = computed(() => taskStore.tasks.find(t => t.id === props.taskId))
const isDualStream = computed(() => {
  return currentTask.value?.input_types?.includes('ir') || false
})

const records = ref<DetectionRecord[]>([])
const pendingRecords = ref<DetectionRecord[]>([])
const loadingRecords = ref(false)
const loadingHistory = ref(false)
const currentPage = ref(1)
const pageSize = 20
let recordsInterval: number | null = null

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

function onVideoPlaying() {
  isVideoActuallyPlaying.value = true;
  streamState.value = 'running'; 
  isVideoPaused.value = false;
  if (!rafId) {
    renderLoop();
  }
}

function onVideoPause() {
  isVideoActuallyPlaying.value = false;
  isVideoPaused.value = true;
  if (rafId) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
}

function onVideoWaiting() {
  isVideoActuallyPlaying.value = false;
  streamState.value = 'loading'; 
  sendDebugLog('VIDEO_WAITING', { msg: 'Buffer empty, waiting...' })
}

function onVideoStalled() {
  sendDebugLog('VIDEO_STALLED', { msg: 'Network stalled, download stopped' })
}

// ========== Phase 3: 检测数据缓冲池 (Glass Overlay) ==========
// 本地数据缓冲池，暂存 WebSocket 或历史 API 传来的检测记录
const detectionBuffer = ref<Array<{ timestamp: number; boxes: any[]; is_history?: boolean }>>([])

// 只保留最近 5 秒的数据（按 30fps 计算约 150 条），防止内存泄漏
function pruneDetectionBuffer() {
  if (detectionBuffer.value.length > 150) {
    detectionBuffer.value = detectionBuffer.value.slice(-150)
  }
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


const clearCanvas = () => {
  const canvas = overlayCanvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height)
}

const showOverlay = computed(() => {
  return streamState.value === 'exception';
});

const shouldShowTimeline = computed(() =>
  ['running', 'connecting', 'retry', 'loading', 'exception', 'paused_end'].includes(streamState.value)
)

const canControl = computed(() => {
  return streamState.value !== 'exception' || playbackMode.value
})

const isActuallyPlaying = computed(() => {
  // UI 按钮状态应直接反映视频底层的暂停/播放状态
  return !isVideoPaused.value;
})

const paginatedRecords = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return records.value.slice(start, start + pageSize)
})

onMounted(async () => {
  isActive = true
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
    } else {
      connect()
    }
    
    initPlayer()
  } catch (e) {
    console.error('Failed to load initial config', e)
    connect()
    initPlayer()
  }

  if (videoPlayerRef.value) {
    resizeObserver = new ResizeObserver(() => {
      syncDisplaySize()
    })
    resizeObserver.observe(videoPlayerRef.value)
  }
})

watch(streamState, (newState) => {
  if (newState === 'running' && !hls && videoPlayerRef.value) {
    sendDebugLog('STATE_RECOVERY_INIT');
    initPlayer();
  }
});

onUnmounted(() => {
  try {
    isActive = false

    if (resizeObserver) {
      resizeObserver.disconnect()
      resizeObserver = null
    }

    if (connectionTimeout) {
      clearTimeout(connectionTimeout)
      connectionTimeout = null
    }

    forceDisconnect()

    destroyPlayer()
    
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }

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
    tasksApi.getDetectionRecords(props.taskId, { limit: 1000 }).then(res => {
      records.value = res.records || []
    }).catch(e => console.warn(e))

    loadHistory()
  }
  initPlayer()
})

watch(() => props.taskId, (newId) => {
  if (newId) {
    forceDisconnect()
    isFrozen.value = false
    playbackMode.value = false
    destroyPlayer()
    connect()
    loadHistory()
    initPlayer()
  }
})

watch(() => currentTask.value?.status, (status) => {
  if (status === 'running' && (streamState.value === 'paused_end' || streamState.value === 'exception')) {
    console.log('[Auto-Sync] Task is running again. Reconnecting...')
    connect()
    initPlayer()
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

    console.log(`[WS Connect] Attempting to connect: ${props.wsUrl}/${props.taskId}`);
    ws = new WebSocket(`${props.wsUrl}/${props.taskId}?token=${token}`)

    ws.onopen = () => {
      console.log('[WS Connected]');
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
        if (data.type !== 'heartbeat' && data.type !== 'detection') {
          console.log(`[WS Message] Received type: ${data.type}`, data);
        }

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
          // 放入缓冲队列，由渲染循环按视频时间同步展示
          if (data.record && !playbackMode.value) {
            pendingRecords.value.push(data.record)
            // 保持缓冲队列不要无限增长
            if (pendingRecords.value.length > 2000) pendingRecords.value.shift()
          }
          break

        case 'detection':
          // 【核心屏障】：如果当前处于历史回放模式，直接丢弃实时推流的数据！
          if (playMode.value === 'history') return;

          // 新架构：接收 AI 推理的实时检测框数据（含绝对时间戳），推入缓冲池
          if (data.payload && data.payload.boxes) {
            detectionBuffer.value.push({
              timestamp: data.payload.timestamp,
              boxes: data.payload.boxes,
              is_history: data.payload.is_history ?? false,
            })
            pruneDetectionBuffer()

            // 同时将记录放入缓冲队列，按帧同步
            const record = data.payload.boxes[0]
            if (record && !playbackMode.value) {
              pendingRecords.value.push({
                id: `ws-${Date.now()}-${Math.random()}`,
                class_name: record.label || 'unknown',
                confidence: record.conf || 0,
                detected_at: new Date(data.payload.timestamp).toISOString(),
                box: [record.x, record.y, record.x + record.w, record.y + record.h],
              } as DetectionRecord)
              if (pendingRecords.value.length > 2000) pendingRecords.value.shift()
            }
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
          break

        case 'retry':
          streamState.value = 'retry'
          retryAttempt.value = data.attempt ?? 1
          retryMax.value = data.max ?? 5
          break

        case 'error':
          streamState.value = 'exception'
          errorMsg.value = data.message || '未知错误'
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

async function initHls(seekToSeconds?: number) {
  if (!videoPlayerRef.value || !isActive) return;

  destroyPlayer();
  
  let streamUrl = '';
  if (playMode.value === 'live') {
    streamUrl = `/storage/${props.taskId}/stream_${currentChannel.value}.m3u8`;
  } else {
    // 关键：历史模式下请求后端生成的定格快照 (VOD Snapshot)
    const endTs = frozenDuration.value || totalDuration.value;
    streamUrl = `/api/tasks/${props.taskId}/vod-stream?channel=${currentChannel.value}&end_time=${endTs}`;
  }

  // 历史模式初始化时设为 loading，实时模式设为 connecting
  streamState.value = playbackMode.value ? 'loading' : 'connecting';

  if (Hls.isSupported()) {
    hls = new Hls({
      enableWorker: true,
      lowLatencyMode: true,
      backBufferLength: 60,
      maxBufferLength: 30,
      manifestLoadingMaxRetry: 10,
    });

    hls.loadSource(streamUrl);
    hls.attachMedia(videoPlayerRef.value);

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      sendDebugLog('HLS_MANIFEST_PARSED');
      if (!videoPlayerRef.value) return;

      const taskStatus = currentTask.value?.status;
      const liveSyncPos = hls?.liveSyncPosition;
      
      if (playMode.value === 'live') {
        if (taskStatus === 'running') {
          videoPlayerRef.value.currentTime = liveSyncPos || 0;
          videoPlayerRef.value.play().catch(() => {});
        } else {
          // 如果任务已停止，强制转为历史回放模式
          playMode.value = 'history';
          playbackMode.value = true;
          frozenDuration.value = hls?.levels[0]?.details?.totalduration || videoPlayerRef.value.duration;
          videoPlayerRef.value.currentTime = 0;
          videoPlayerRef.value.play().catch(() => {});
          fetchHistoricalRecords(0, frozenDuration.value);
        }
      } else {
        // 历史回放模式
        if (seekToSeconds !== undefined) {
          videoPlayerRef.value.currentTime = seekToSeconds;
        }
        videoPlayerRef.value.play().catch(() => {});
      }
      
      isSwitchingStream = false;
    });

    hls.on(Hls.Events.FRAG_CHANGED, (event, data) => {
      if (data.frag.programDateTime) {
        currentFragProgramDateTime.value = data.frag.programDateTime;
        currentFragVideoTime.value = data.frag.start;
        
        // 记录流的绝对起始参考点
        if (firstSessionStartTime.value === null) {
          firstSessionStartTime.value = data.frag.programDateTime - (data.frag.start * 1000);
        }
      }
    });

    hls.on(Hls.Events.ERROR, (event, data) => {
      if (data.fatal) {
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            console.error("HLS Network Error:", data);
            hls?.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            console.error("HLS Media Error:", data);
            hls?.recoverMediaError();
            break;
          default:
            destroyPlayer();
            break;
        }
      }
    });
  } else if (videoPlayerRef.value.canPlayType('application/vnd.apple.mpegurl')) {
    videoPlayerRef.value.src = streamUrl;
    videoPlayerRef.value.play().catch(() => {});
  }
}

// 保持对 initPlayer 的引用，防止外部调用失效（虽然通常是内部调用）
const initPlayer = initHls;

function destroyPlayer() {
  if (hls) {
    hls.destroy();
    hls = null;
  }
  isVideoActuallyPlaying.value = false;
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

      if (merged.first_session_start_time) {
        firstSessionStartTime.value = new Date(merged.first_session_start_time).getTime();
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

// fetchTaskStartTime is deprecated as we use programDateTime

// ========== Phase 3: 核心渲染引擎 (requestAnimationFrame) ==========
// 放弃依赖 WebSocket 的触发，改为根据视频当前的物理进度主动去缓冲池里"捞"框
function renderLoop() {
  if (!isActive || !videoPlayerRef.value || !overlayCanvasRef.value) return;

  const video = videoPlayerRef.value;
  const canvas = overlayCanvasRef.value;

  // 同步 Canvas 尺寸
  syncDisplaySize();

  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  // 1. 清空画布
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // 2. 获取视频当前正在播放画面的"绝对时间" (毫秒)
  let currentVideoAbsTime: number;
  if (hls?.playingDate) {
    currentVideoAbsTime = hls.playingDate.getTime();
  } else if (firstSessionStartTime.value) {
    currentVideoAbsTime = firstSessionStartTime.value + (video.currentTime * 1000);
  } else if (frozenRealTime.value) {
    // 降级：如果是历史模式但没有打时间戳，使用冻结时推算的锚点
    currentVideoAbsTime = frozenRealTime.value + (video.currentTime * 1000);
  } else {
    currentVideoAbsTime = Date.now();
  }

  // --- 2.1 记录同步逻辑：将待展示队列中时间已到的记录推入侧边栏 ---
  if (!playbackMode.value && pendingRecords.value.length > 0 && isVideoActuallyPlaying.value) {
    let movedCount = 0;
    while (pendingRecords.value.length > 0) {
      const first = pendingRecords.value[0];
      const recordTime = new Date(first.detected_at).getTime();
      
      // 如果记录时间已经到达或早于当前画面时间，则展示它
      if (recordTime <= currentVideoAbsTime) {
        const record = pendingRecords.value.shift();
        if (record) {
          records.value.unshift(record);
          movedCount++;
        }
      } else {
        break;
      }
    }
    if (movedCount > 0 && records.value.length > 1000) {
      records.value = records.value.slice(0, 1000);
    }
  }

  // 3. 去缓冲池里，找与视频当前时间最接近的那一条记录（允许前后 500ms 误差）
  let closestRecord: { timestamp: number; boxes: any[] } | null = null;
  let minDiff = Infinity;

  for (const record of detectionBuffer.value) {
    const diff = Math.abs(record.timestamp - currentVideoAbsTime);
    if (diff < 500 && diff < minDiff) {
      minDiff = diff;
      closestRecord = record;
    }
  }

  // 4. 画框！
  if (closestRecord && closestRecord.boxes.length > 0) {
    const naturalW = video.videoWidth || canvas.width;
    const naturalH = video.videoHeight || canvas.height;

    const imgAspect = naturalW / naturalH;
    const canvasAspect = canvas.width / canvas.height;

    let renderW: number, renderH: number, offsetX: number, offsetY: number;

    if (imgAspect > canvasAspect) {
      renderW = canvas.width;
      renderH = canvas.width / imgAspect;
      offsetX = 0;
      offsetY = (canvas.height - renderH) / 2;
    } else {
      renderH = canvas.height;
      renderW = canvas.height * imgAspect;
      offsetX = (canvas.width - renderW) / 2;
      offsetY = 0;
    }

    const scaleX = renderW / naturalW;
    const scaleY = renderH / naturalH;

    closestRecord.boxes.forEach((box: any) => {
      const x = offsetX + box.x * scaleX;
      const y = offsetY + box.y * scaleY;
      const w = box.w * scaleX;
      const h = box.h * scaleY;

      ctx.strokeStyle = '#ff4d4f';
      ctx.lineWidth = 2;
      ctx.strokeRect(x, y, w, h);

      const label = `${box.label || 'unknown'} ${Math.round((box.conf || 0) * 100)}%`;
      ctx.font = '12px Inter, sans-serif';
      const labelWidth = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(255, 77, 79, 0.85)';
      ctx.fillRect(x, Math.max(0, y - 20), labelWidth + 10, 20);

      ctx.fillStyle = 'white';
      ctx.fillText(label, x + 5, Math.max(14, y - 6));
    });
  }

  // 保持高达 60FPS 的丝滑渲染循环
  rafId = requestAnimationFrame(renderLoop);
}

function clearCanvas() {
  const canvas = overlayCanvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function onTimeUpdate(e: Event) {
  const video = e.target as HTMLVideoElement;
  
  // 🚨 强制拦截：如果是历史模式，且播放时间已经达到定格的最大时间
  if (playMode.value === 'history' && frozenDuration.value > 0) {
    if (video.currentTime >= frozenDuration.value - 0.2) {
      video.pause();
      return; 
    }
  }

  // 核心修复：如果是无穷大或者 NaN，说明 HLS 还没准备好 DVR 窗口
  if (isNaN(video.duration) || !isFinite(video.duration)) {
    const currentLevel = hls?.currentLevel ?? -1;
    const levelDetails = (currentLevel >= 0) ? hls?.levels?.[currentLevel]?.details : null;
    
    if (levelDetails) {
      totalDuration.value = levelDetails.totalduration;
    }
  } else {
    totalDuration.value = video.duration;
  }

  if (!isUserSeeking.value) {
    currentGlobalTime.value = video.currentTime;
  }

  // 【滑动窗口机制】：如果在历史模式下，且视频播放快到了我们当前数据的边缘
  if (playMode.value === 'history' && firstSessionStartTime.value && !isSwitchingStream) {
    const currentPhysicalTimeMs = firstSessionStartTime.value + (video.currentTime * 1000);
    
    // 如果当前播放时间距离我们抓取的数据边界不到 5 秒了，悄悄在后台拉取下一个 30 秒的切片
    if (historyLastFetchTime - currentPhysicalTimeMs < 5000) {
        loadHistoricalBoxes(historyLastFetchTime, historyLastFetchTime + 30000);
    }
  }
}

function toggleChannel() {
  if (!hls || !videoPlayerRef.value) return;

  // 记录切换瞬间的状态
  const timeToRestore = videoPlayerRef.value.currentTime;
  const isPaused = videoPlayerRef.value.paused;

  // 切换通道状态
  currentChannel.value = currentChannel.value === 'rgb' ? 'ir' : 'rgb';
  
  // 生成新的 URL (根据 tasks.py 中的新路由)
  const newStreamUrl = `/storage/${props.taskId}/stream_${currentChannel.value}.m3u8`;

  // 直接加载新流
  hls.loadSource(newStreamUrl);

  // 监听新流解析完成，瞬间恢复时间
  hls.once(Hls.Events.MANIFEST_PARSED, () => {
    if (videoPlayerRef.value) {
      videoPlayerRef.value.currentTime = timeToRestore;
      if (!isPaused) {
        videoPlayerRef.value.play().catch(() => {});
      }
    }
  });
  
  message.success(`已切换至 ${currentChannel.value === 'rgb' ? '可见光' : '红外'} 画面`);
  sendDebugLog('CHANNEL_SWAP', { channel: currentChannel.value, time: timeToRestore });
}

function handleSeekBarHover(e: MouseEvent) {
  if (!seekbarWrapRef.value || totalDuration.value <= 0) return;

  const rect = seekbarWrapRef.value.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const percent = Math.max(0, Math.min(1, x / rect.width));
  
  // 核心修复：直接使用总时长百分比计算相对秒数
  const hoverSeconds = percent * (frozenDuration.value || totalDuration.value);

  hoverX.value = x;
  showHoverTooltip.value = true;
  
  // 必须使用 formatDuration (格式化秒数)，绝对不能传入 Epoch 时间戳！
  hoverTimeAbs.value = formatDuration(hoverSeconds); 
}

watch(records, () => {
  // Handled by renderLoop
}, { deep: true })

async function fetchHistoricalRecords(startSec: number, endSec: number) {
  if (!props.taskId) return
  try {
    // 核心修复：根据绝对起始时间，将相对秒数转换为绝对时间戳
    const baseTs = firstSessionStartTime.value || (Date.now() - totalDuration.value * 1000);
    const absStart = baseTs + startSec * 1000;
    const absEnd = baseTs + endSec * 1000;

    const res = await tasksApi.getDetectionRecords(props.taskId, {
      start_time: new Date(absStart).toISOString(),
      end_time: new Date(absEnd).toISOString(),
      limit: 5000,
    })

    // 将后端的历史数据转换为缓冲池格式
    const historicalData = (res.records || []).map((rec: DetectionRecord) => ({
      timestamp: new Date(rec.detected_at).getTime(),
      boxes: rec.box ? [{
        x: rec.box[0],
        y: rec.box[1],
        w: rec.box[2] - rec.box[0],
        h: rec.box[3] - rec.box[1],
        conf: rec.confidence,
        label: rec.class_name,
      }] : [],
      is_history: true,
    }))

    // 合并到缓冲池，去重（基于 timestamp）
    const existingTimestamps = new Set(detectionBuffer.value.map(d => d.timestamp))
    const newData = historicalData.filter((d: any) => !existingTimestamps.has(d.timestamp))
    detectionBuffer.value.push(...newData)
    pruneDetectionBuffer()

    sendDebugLog('FETCH_HISTORY_RECORDS', { count: newData.length, range: [startSec, endSec] })
  } catch (e) {
    console.warn('[History Records] Failed to fetch:', e)
  }
}

// 历史滑动窗口边界标记
let historyLastFetchTime = 0;

async function loadHistoricalBoxes(startMs: number, endMs: number) {
  if (!props.taskId) return;
  try {
    const res = await tasksApi.getDetections(props.taskId, {
      start_time: startMs,
      end_time: endMs
    });

    if (res && res.length > 0) {
      // 合并并去重（基于 timestamp）
      const existingTs = new Set(detectionBuffer.value.map(d => d.timestamp));
      const newData = res.filter((d: any) => !existingTs.has(d.timestamp));
      detectionBuffer.value = [...detectionBuffer.value, ...newData].sort((a, b) => a.timestamp - b.timestamp);
    }
    historyLastFetchTime = Math.max(historyLastFetchTime, endMs);
    sendDebugLog('LOAD_HISTORICAL_BOXES', { count: res?.length, range: [startMs, endMs] });
  } catch (e) {
    console.error("Failed to load historical boxes", e);
  }
}

// 真正的流切换执行逻辑
const executeStreamSwitch = async (targetSeconds: number) => {
    isSwitchingStream = true;
    
    // 1. 【核心动作】：清空过期的缓冲池和画布！
    detectionBuffer.value = [];
    clearCanvas();
    
    // 计算跳跃目标的绝对 UTC 时间戳 (毫秒)
    const currentBase = firstSessionStartTime.value || (Date.now() - totalDuration.value * 1000);
    const targetPhysicalTimeMs = currentBase + (targetSeconds * 1000);

    if (playMode.value === 'live') {
        console.log("【时空切分】冻结直播时间线，进入历史模式...");
        playMode.value = 'history';
        playbackMode.value = true;
        frozenDuration.value = totalDuration.value;
        frozenRealTime.value = currentBase + (targetSeconds * 1000);
        
        // 重新初始化播放器，加载 VOD Snapshot
        initHls(targetSeconds);
    } else {
        // 已经在历史模式，直接 seek 即可
        if (videoPlayerRef.value) {
            videoPlayerRef.value.currentTime = targetSeconds;
            videoPlayerRef.value.play().catch(()=>{});
        }
        isSwitchingStream = false;
    }
    
    // 2. 预加载历史检测框 (抓取拖拽点前后一段时间的数据)
    await loadHistoricalBoxes(targetPhysicalTimeMs - 5000, targetPhysicalTimeMs + 30000);
    
    if (playMode.value === 'history') {
      isSwitchingStream = false;
    }
};

// 暴露给进度条的防抖处理器 (延迟 400ms，拖拽滑块期间不发请求)
const debouncedSeek = debounce((targetSeconds: number) => {
    executeStreamSwitch(targetSeconds);
}, 400);

function handleGlobalSeek(targetPercent: number) {
  if (!videoPlayerRef.value || isNaN(targetPercent)) return;
  
  const targetSeconds = (targetPercent / 100) * (frozenDuration.value || totalDuration.value);
  
  // UI 层立即给反馈
  isUserSeeking.value = true; 
  currentGlobalTime.value = targetSeconds;
  
  // 放入防抖队列
  debouncedSeek(targetSeconds);
}

function handleUnifiedPlayPause() {
  if (!videoPlayerRef.value) return;

  if (videoPlayerRef.value.paused) {
    // 恢复播放
    if (playbackMode.value && videoPlayerRef.value.currentTime >= videoPlayerRef.value.duration - 0.5) {
      videoPlayerRef.value.currentTime = 0;
    }
    videoPlayerRef.value.play().catch(e => {
      console.error("Playback failed:", e);
      message.error("播放失败");
    });
    // @playing 事件会同步更新 isVideoPaused.value = false
  } else {
    // 暂停播放
    videoPlayerRef.value.pause();
    // @pause 事件会同步更新 isVideoPaused.value = true
  }
}

function jumpToLive() {
  const task = currentTask.value;
  if (task && task.status !== 'running') {
    message.warning("任务已停止，请先执行任务连接视频源！");
    return;
  }

  // 解除历史锁定，切回直播模式
  playMode.value = 'live';
  playbackMode.value = false;
  isUserSeeking.value = false;
  frozenDuration.value = 0;
  frozenRealTime.value = 0;
  
  sendDebugLog('JUMP_TO_LIVE');

  // 重新加载原始直播流
  initHls();
}

// Deprecated methods removed

function handleVideoEnded() {
  if (videoPlayerRef.value) {
    videoPlayerRef.value.pause();
  }
  isVideoPaused.value = true;
  streamState.value = 'paused_end'; 
  message.info("已播放至历史记录最末端");
  sendDebugLog('VIDEO_ENDED')
}

function tipFormatter(val: number) {
  return formatDuration((val / 100) * (frozenDuration.value || totalDuration.value));
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
    initPlayer()
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
}

function formatTime(isoString: string): string {
  const date = new Date(isoString)
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

function formatTimeFromTs(ts: number): string {
  const date = new Date(ts);
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
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
  background-color: #000000 !important; /* 强制视频标签在无画面时为纯黑，杜绝白闪 */
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
  z-index: 100; /* Exception overlay should be on top of everything */
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: rgba(10, 10, 20, 0.95);
  backdrop-filter: blur(10px);
}

.video-overlay-inner {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(12px) saturate(180%);
  border: 1px solid rgba(255, 255, 255, 0.05);
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
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
  padding: 14px 24px;
  background: linear-gradient(to top, rgba(0,0,0,0.8) 0%, rgba(0,0,0,0.4) 100%);
  backdrop-filter: blur(20px);
  display: flex;
  align-items: center;
  gap: 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 30;
  box-shadow: 0 -10px 30px rgba(0,0,0,0.5);
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
