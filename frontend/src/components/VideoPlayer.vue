<template>
  <div class="video-player-container" role="region" aria-label="视频播放器">
    <div v-if="showOverlay" class="overlay">
      <template v-if="streamState === 'exception'">
        <div class="state-icon exception-icon">&#9888;</div>
        <div class="state-label">{{ errorMsg || '连接中断' }}</div>
        <div class="state-sub">{{ errorMsg ? '请根据提示检查环境配置' : '请检查网络或查看历史录像' }}</div>
      </template>
    </div>

    <div class="frame-container">
      <video
        v-show="playbackMode === 'hls'"
        ref="videoPlayerRef"
        autoplay
        :muted="isMuted"
        playsinline
        preload="auto"
        class="video-frame"
        :class="{ 'playback-video': playMode === 'history' }"
        @timeupdate="onTimeUpdate"
        @ended="handleVideoEnded"
        @playing="onVideoPlaying"
        @pause="onVideoPause"
        @waiting="onVideoWaiting"
        @error="onVideoError"
        @seeked="onVideoSeeked"
      ></video>

      <WebRTCPlayer
        v-if="playbackMode === 'webrtc' && webrtcStreamPath"
        ref="webrtcPlayerRef"
        :stream-path="webrtcStreamPath"
        class="video-frame"
        :class="{ 'playback-video': playMode === 'history' }"
        @playing="onWebRTCPlaying"
        @error="onWebRTCError"
      />

      <!-- V4.6: 全模式使用内嵌方案，检测框由 AnnotatedHLSWriter 嵌入 HLS 视频流，前端无 Canvas -->

      <!-- 【P2 修复】：检测数据过期提示 -->
      <div v-if="isDetectionStale && streamState === 'running'" class="stale-detection-badge">
        <span class="stale-dot"></span>
        <span class="stale-text">检测恢复中</span>
      </div>

      <!-- Video Loading Overlay -->
      <div v-if="showVideoOverlay" class="video-overlay-inner">
        <template v-if="streamState === 'connecting'">
          <div class="state-icon connecting-pulse"></div>
          <div class="state-label">正在连接视频服务...</div>
        </template>
        <template v-else-if="streamState === 'loading' || streamState === 'model_loading'">
          <div class="state-icon loading-bar-wrap">
            <div class="loading-bar"></div>
          </div>
          <div class="state-label">正在准备监控画面...</div>
        </template>
        <template v-else-if="streamState === 'retry'">
          <div class="state-icon retry-spin"></div>
          <div class="state-label">视频源连接波动，正在重试 ({{ retryAttempt }}/{{ retryMax }})</div>
        </template>
        <template v-else-if="streamState === 'buffering'">
          <div class="state-icon loading-bar-wrap">
            <div class="loading-bar"></div>
          </div>
          <div class="state-label">画面加载中，请稍候...</div>
        </template>
        <template v-else-if="streamState === 'running' && !isVideoActuallyPlaying">
          <div class="state-icon loading-bar-wrap">
            <div class="loading-bar"></div>
          </div>
          <div class="state-label">画面加载中，请稍候...</div>
        </template>
        <template v-else-if="streamState === 'recovering'">
          <div class="state-icon connecting-pulse"></div>
          <div class="state-label">网络不稳定，尝试重连中...</div>
        </template>
      </div>

      <div class="controls" v-if="canControl">
        <div class="mode-tag" :class="{ 'is-live': playMode !== 'history' }">
          {{ playMode === 'history' ? '历史回放' : '实时监控' }}
        </div>

        <a-divider type="vertical" border-color="rgba(255,255,255,0.2)" />

        <div class="mode-tag" :class="{ 'is-webrtc': playbackMode === 'webrtc' }">
          {{ playbackMode === 'webrtc' ? 'WebRTC' : 'HLS' }}
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
              v-show="playMode === 'history'"
              shape="circle"
              @click="jumpToLive"
              class="rollback-btn"
              style="margin-left: 12px;"
            >
              <template #icon><RollbackOutlined /></template>
            </a-button>
          </a-tooltip>

          <a-tooltip :title="currentChannel === 'annotated' ? '切换到IR标注流' : '切换到RGB标注流'">
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

    <div class="detection-records-panel" v-show="showRecordsPanel">
      <DetectionEventsPanel
        :task-id="taskId"
        :events="detectionEvents"
        :active-tracks="activeTracks"
      />
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
import { logger } from '@/utils/logger'
import { diagLogger } from '@/utils/diagLogger'
import WebRTCPlayer from '@/components/WebRTCPlayer.vue'
import DetectionEventsPanel from '@/components/DetectionEventsPanel.vue'
import {
  PauseOutlined,
  CaretRightOutlined,
  RollbackOutlined,
  SwapOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'

type StreamState = 'connecting' | 'retry' | 'loading' | 'model_loading' | 'running' | 'exception' | 'recovering' | 'paused_end' | 'buffering'

const props = defineProps<{
  taskId: string
  wsUrl: string
  showRecordsPanel?: boolean
}>()

const emit = defineEmits(['close', 'status-change', 'time-update', 'playback-absolute-time'])

const videoPlayerRef = ref<HTMLVideoElement | null>(null)
const webrtcPlayerRef = ref<InstanceType<typeof WebRTCPlayer> | null>(null)
const streamState = ref<StreamState>('connecting')
const isFrozen = ref(false)
const errorMsg = ref('')
const retryAttempt = ref(0)
const retryMax = ref(5)
const resuming = ref(false)
const isMuted = ref(true)

const playbackMode = ref<'hls' | 'webrtc'>('hls')
const webrtcStreamPath = ref('')
const webrtcLatencyMs = ref(0)

const taskStore = useTaskStore()

let ws: WebSocket | null = null
let isActive = true
let historyController: AbortController | null = null
let rafId: number | null = null
let connectionTimeout: any = null
const wsReconnectCount = ref(0)
const WS_MAX_RECONNECT = 5
let detectionStaleReconnectTimer: number | null = null
const STALE_RECONNECT_THRESHOLD_MS = 20000
let lastRecoveredTime = 0
const RECOVERED_DEBOUNCE_MS = 3000

const playMode = ref<'live' | 'history'>('live')
const frozenDuration = ref(0)
const frozenRealTime = ref(0)
let isSwitchingStream = false
let pendingSeekSeconds: number | undefined = undefined

let hls: Hls | null = null;
const isVideoActuallyPlaying = ref(false);

const showRecordsPanel = computed(() => {
  if (props.showRecordsPanel !== undefined) return props.showRecordsPanel
  return ['connecting', 'retry', 'loading', 'model_loading', 'running', 'recovering', 'buffering', 'paused_end', 'exception'].includes(streamState.value)
})

const isVideoPaused = ref(false);
const isVideoReady = ref(false);
const lastConfirmedPlayingTime = ref(0);

const showVideoOverlay = computed(() => {
  if (streamState.value === 'exception') return false;
  
  const loadingStates: StreamState[] = ['connecting', 'retry', 'loading', 'model_loading', 'recovering'];
  
  if (loadingStates.includes(streamState.value)) return true;

  if (playbackMode.value === 'webrtc') return false;

  if (isVideoPaused.value) return false;
  
  if (streamState.value === 'running') {
    const video = videoPlayerRef.value;
    if (!video) return true;

    const recentlyPlaying = (Date.now() - lastConfirmedPlayingTime.value) < 5000;
    if (recentlyPlaying) return false;

    if (video.readyState < 2 || !isVideoActuallyPlaying.value) {
      return true;
    }
  }

  if (streamState.value === 'buffering') {
    const recentlyPlaying = (Date.now() - lastConfirmedPlayingTime.value) < 5000;
    if (recentlyPlaying) return false;
    return true;
  }

  return false;
});


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
const sessionStartTime = ref<number>(0); 
const sessionMap = ref<Array<{ index: number; start_offset_sec: number; start_abs_time_ms: number }>>([]);

function offsetToAbsTime(offsetSec: number): number {
  if (sessionMap.value.length === 0) {
    return (firstSessionStartTime.value || 0) + offsetSec * 1000;
  }
  let matched = sessionMap.value[0];
  for (let i = sessionMap.value.length - 1; i >= 0; i--) {
    if (offsetSec >= sessionMap.value[i].start_offset_sec) {
      matched = sessionMap.value[i];
      break;
    }
  }
  const offsetWithinSession = offsetSec - matched.start_offset_sec;
  return matched.start_abs_time_ms + offsetWithinSession * 1000;
}

let lastVideoAbsTime = 0
let lastVideoAbsTimeTs = 0
let cachedBufferDelayMs = 6000
const HLS_FIXED_LATENCY_MS = 3000

// 【前端自动校准】：微调播放器缓冲波动
let timeCalibrationMs = 0
let calibrationInitialized = false
let calibrationSampleCount = 0
let calibrationLastResetTime = 0

// 【NTP 式动态校准 - 增强】：EWMA + 异常值过滤
const calibrationHistory: number[] = []
const CALIBRATION_HISTORY_MAX = 100
const CALIBRATION_OUTLIER_STD_THRESHOLD = 3  // 3 倍标准差过滤

// 【NTP 偏移量监控】：仅用于诊断日志，不参与时间计算
let ntpOffsetMs = 0
let ntpSynced = false

// 校准参数：适应 WSL2 时钟偏差和长会话偏移
const CALIBRATION_EWMA_ALPHA = 0.1
const CALIBRATION_SYNC_WINDOW_MS = 8000     // 匹配窗口 8s（原 5s），应对大偏移
const CALIBRATION_CLAMP_MS = 15000           // 样本 clamp ±15s（原 5s），允许收集宽范围样本
const CALIBRATION_MAX_RATE_MS = 200          // 允许更大漂移速率（原 50）
const CALIBRATION_MIN_REASONABLE = -10000    // 校准下限 -10s（原 -3s）
const CALIBRATION_MAX_REASONABLE = 10000     // 校准上限 +10s（原 3s）
const CALIBRATION_MIN_SAMPLES = 3
const CALIBRATION_RESET_COOLDOWN = 30000
const FIXED_OFFSET_MS = 100
const EXTRAPOLATION_MAX_MS = 5000
const MAX_RECORDS_PER_SECOND = 30
const FORCE_RELEASE_THRESHOLD_MS = 5000
let recordsReleasedThisSecond = 0
let recordsReleaseSecondStart = 0
let lastRecordsReleaseDiag = 0
let lastEmittedAbsoluteTime = 0

// 过滤超过 3 倍标准差的样本
function isOutlier(sample: number): boolean {
  if (calibrationHistory.length < 10) {
    return false;  // 样本不足，不过滤
  }
  const mean = calibrationHistory.reduce((a, b) => a + b, 0) / calibrationHistory.length;
  const variance = calibrationHistory.reduce((sum, v) => sum + (v - mean) ** 2, 0) / calibrationHistory.length;
  const std = Math.sqrt(variance);
  return Math.abs(sample - mean) > CALIBRATION_OUTLIER_STD_THRESHOLD * std;
}

function addCalibrationSample(sample: number) {
  calibrationHistory.push(sample);
  if (calibrationHistory.length > CALIBRATION_HISTORY_MAX) {
    calibrationHistory.shift();
  }
}

let _getVideoAbsTimeLogCounter = 0;
let _lastAbsTimeSource = '';

function getVideoAbsTime(video: HTMLVideoElement | null): number {
  if (isSwitchingStream && frozenRealTime.value) {
    return frozenRealTime.value;
  }

  if (playbackMode.value === 'webrtc' && playMode.value === 'live') {
    return Date.now() + clockOffset - FIXED_OFFSET_MS - webrtcLatencyMs.value;
  }

  // V4.10: HLS programDateTime 优先 — 每段 TS 分片携带绝对时间戳，
  // 与后端 FFmpeg 时钟对齐，精度远超 sessionStartTime 推算。
  const currentTime = video ? video.currentTime : 0;
  const currentLvl = hls?.currentLevel;
  const levelIdx = currentLvl !== undefined && currentLvl >= 0 ? currentLvl : 0;
  const details = hls?.levels?.[levelIdx]?.details;

  if (details) {
    const fragments = details.fragments || [];
    const currentFrag = fragments.find(
      (f: any) => currentTime >= f.start - 0.05 && currentTime < f.start + f.duration + 0.05
    );
    if (currentFrag?.programDateTime) {
      const pdtMs = typeof currentFrag.programDateTime === 'number'
        ? currentFrag.programDateTime
        : new Date(currentFrag.programDateTime).getTime();
      const t = pdtMs + (currentTime - currentFrag.start) * 1000;
      lastVideoAbsTime = t;
      lastVideoAbsTimeTs = Date.now();
      cachedBufferDelayMs = Date.now() + clockOffset - t;
      _lastAbsTimeSource = 'pdt';
      return t;
    }

    // 防御性外推 1：currentTime 尚未达到首个分片（如初始加载时）
    if (fragments.length > 0) {
      const firstFrag = fragments[0];
      if (currentTime < firstFrag.start && firstFrag.programDateTime) {
        const pdtMs = typeof firstFrag.programDateTime === 'number'
          ? firstFrag.programDateTime
          : new Date(firstFrag.programDateTime).getTime();
        const t = pdtMs - (firstFrag.start - currentTime) * 1000;
        lastVideoAbsTime = t;
        lastVideoAbsTimeTs = Date.now();
        cachedBufferDelayMs = Date.now() + clockOffset - t;
        _lastAbsTimeSource = 'pdt_approx_past';
        return t;
      }
    }

    // 防御性外推 2：currentTime 超过末尾分片（如临近直播边缘）
    if (fragments.length > 0) {
      const lastFrag = fragments[fragments.length - 1];
      if (currentTime >= lastFrag.start + lastFrag.duration && lastFrag.programDateTime) {
        const pdtMs = typeof lastFrag.programDateTime === 'number'
          ? lastFrag.programDateTime
          : new Date(lastFrag.programDateTime).getTime();
        const t = pdtMs + (currentTime - lastFrag.start) * 1000;
        lastVideoAbsTime = t;
        lastVideoAbsTimeTs = Date.now();
        cachedBufferDelayMs = Date.now() + clockOffset - t;
        _lastAbsTimeSource = 'pdt_approx_future';
        return t;
      }
    }
  }

  if (hls?.playingDate) {
    const t = hls.playingDate.getTime();
    lastVideoAbsTime = t;
    lastVideoAbsTimeTs = Date.now();
    cachedBufferDelayMs = Date.now() + clockOffset - t;
    _lastAbsTimeSource = 'playingDate';
    return t;
  }

  // 回退: sessionStartTime 相对计算（PDT 不可用时）
  if (firstSessionStartTime.value && sessionStartTime.value > 0) {
    const absTime = sessionStartTime.value + currentTime * 1000;
    lastVideoAbsTime = absTime;
    lastVideoAbsTimeTs = Date.now();
    _lastAbsTimeSource = 'relative';
    return absTime;
  }
  if (firstSessionStartTime.value || sessionMap.value.length > 0) {
    const absTime = offsetToAbsTime(currentTime);
    _lastAbsTimeSource = 'session';
    return absTime - HLS_FIXED_LATENCY_MS;
  }
  if (lastVideoAbsTime > 0 && (Date.now() - lastVideoAbsTimeTs) < EXTRAPOLATION_MAX_MS) {
    const t = lastVideoAbsTime + (Date.now() - lastVideoAbsTimeTs);
    if (_lastAbsTimeSource !== 'extrapolate') {
      diagLogger.log('DIAG-ABSTIME', JSON.stringify({ source: 'extrapolate_fallback', t, prevSource: _lastAbsTimeSource }));
    }
    _lastAbsTimeSource = 'extrapolate';
    return t;
  }

  // 终极回退：动态利用 hls.latency 评估出最准确的当前播放画面真实延迟
  let currentDelay = cachedBufferDelayMs;
  if (playbackMode.value === 'hls' && hls && hls.latency >= 0 && hls.latency < 60) {
    currentDelay = hls.latency * 1000;
    cachedBufferDelayMs = currentDelay;
  }
  const t = Date.now() + clockOffset - currentDelay;
  if (_lastAbsTimeSource !== 'final') {
    diagLogger.log('DIAG-ABSTIME', JSON.stringify({ source: 'final_fallback', t, currentDelay, prevSource: _lastAbsTimeSource }));
  }
  _lastAbsTimeSource = 'final';
  return t;
}

const currentGlobalTime = ref(0)
const totalDuration = ref(0)
const isUserSeeking = ref(false)
let pendingSeekTime: number | null = null

const seekbarWrapRef = ref<HTMLElement | null>(null)
const showHoverTooltip = ref(false)
const hoverX = ref(0)
const hoverTimeAbs = ref('')

let liveTimeTimer: number | null = null
let liveTimeBase = 0

let runningSecondsBase = 0
let runningSecondsTimer: number | null = null
let runningSecondsStartTime = 0

function startRunningSecondsCounter(baseSeconds: number) {
  stopRunningSecondsCounter()
  runningSecondsBase = baseSeconds
  runningSecondsStartTime = Date.now()
  totalDuration.value = baseSeconds
  runningSecondsTimer = window.setInterval(() => {
    // totalDuration 代表会话总时长，不受 playMode 影响，在任何模式下都持续递增
    const elapsed = (Date.now() - runningSecondsStartTime) / 1000
    totalDuration.value = runningSecondsBase + elapsed
    emit('time-update', totalDuration.value)
    // 同步左侧时间：不区分 playMode，直接从 video 读取
    if (videoPlayerRef.value && !isUserSeeking.value && !isSwitchingStream) {
      currentGlobalTime.value = videoPlayerRef.value.currentTime
    }
  }, 200)
}

function stopRunningSecondsCounter() {
  if (runningSecondsTimer) {
    clearInterval(runningSecondsTimer)
    runningSecondsTimer = null
  }
}

function startLiveTimeCounter() {
  if (liveTimeTimer) return
  liveTimeBase = Date.now()
  liveTimeTimer = window.setInterval(() => {
    liveTimeBase = Date.now()
  }, 1000)
}

function stopLiveTimeCounter() {
  if (liveTimeTimer) {
    clearInterval(liveTimeTimer)
    liveTimeTimer = null
  }
}

// Dual-channel support
const currentChannel = ref<'annotated' | 'rgb' | 'ir' | 'ir_annotated'>('annotated')
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

// V3.0: 事件驱动检测记录
const detectionEvents = ref<any[]>([])
const activeTracks = ref(0)
let eventsPollingTimer: number | null = null
let clockCalibrationInterval: number | null = null
let bufferCleanupTimer: number | null = null

const currentDetectionConfig = ref<any>(null)

let resizeObserver: ResizeObserver | null = null
let lastDriftLog = 0
let lastRenderDiagLog = 0
let lastDimWarnLog = 0
let clockOffset = 0
let isDetectionStale = ref(false)
let lastStaleLog = 0
let lastDetectionMessageTime = Date.now()

// 【P0-4 诊断日志】：lastValidRecord 超时清除机制
let lastValidRecordUpdateTime = 0
const LAST_VALID_RECORD_TIMEOUT_MS = 5000  // 5 秒无新检测框则清除

const sendDebugLog = (event_type: string, details: any = {}) => {
  if (!props.taskId) return
  tasksApi.logPlayback(props.taskId, event_type, {
    playbackMode: playMode.value === 'history',
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
function switchToWebRTC() {
  if (playMode.value === 'history') return;
  if (playbackMode.value === 'webrtc') return;
  const streamPath = `fg_${props.taskId}_rgb`;
  webrtcStreamPath.value = streamPath;
  playbackMode.value = 'webrtc';
  logger.info('webrtc', 'Switching to WebRTC mode', { taskId: props.taskId, streamPath });
}

function switchToHLS() {
  if (playbackMode.value === 'hls') return;
  playbackMode.value = 'hls';
  webrtcStreamPath.value = '';
  logger.info('webrtc', 'Switching to HLS mode', { taskId: props.taskId });
}

function onWebRTCPlaying() {
  logger.info('webrtc', 'WebRTC playback started', { taskId: props.taskId });
  isVideoActuallyPlaying.value = true;
  isVideoReady.value = true;
  lastConfirmedPlayingTime.value = Date.now();
}

function onWebRTCError(error: any) {
  logger.error('webrtc', 'WebRTC playback error, falling back to HLS', { taskId: props.taskId, error });
  switchToHLS();
  if (!hls) {
    initHls();
  }
}

function onVideoPlaying() {
  isVideoActuallyPlaying.value = true;
  isVideoReady.value = true;
  lastConfirmedPlayingTime.value = Date.now();
  if (streamState.value === 'buffering') {
    streamState.value = 'running';
  }
  isVideoPaused.value = false;
  stopLiveTimeCounter();

  setTimeout(() => {
    if (isMuted.value) {
      isMuted.value = false;
      logger.info('stream', 'Smart unmute attempted on playing');
    }
  }, 500);

  if (!rafId) {
    renderLoop();
  }
}

function onVideoSeeked() {
  if (isSwitchingStream) {
    isSwitchingStream = false;
    detectionBuffer.value = [];
    lastValidRecord.value = null;
    logger.info('playback', 'Seek completed, resuming detection rendering', {
      taskId: props.taskId,
      currentTime: videoPlayerRef.value?.currentTime?.toFixed(2)
    });
  }
}

function onVideoPause() {
  isVideoActuallyPlaying.value = false;
  isVideoPaused.value = true;
  const activeStates: StreamState[] = ['running', 'buffering', 'recovering'];
  if (playMode.value === 'live' && activeStates.includes(streamState.value)) {
    startLiveTimeCounter();
  }
}

function onVideoWaiting() {
  isVideoActuallyPlaying.value = false;
  // 改造：视频缓冲用 'buffering' 状态，不再污染 'loading'
  if (streamState.value === 'running') {
    streamState.value = 'buffering';
  }
  sendDebugLog('VIDEO_WAITING', { msg: 'Buffer empty, waiting...' })
}

function onVideoStalled() {
  sendDebugLog('VIDEO_STALLED', { msg: 'Network stalled, download stopped' })
}

// ========== Phase 3: 检测数据缓冲池 (Glass Overlay) ==========
// 本地数据缓冲池，暂存 WebSocket 或历史 API 传来的检测记录
const detectionBuffer = ref<Array<{ timestamp: number; timestamp_ms?: number; wall_clock?: number; boxes: any[]; is_history?: boolean }>>([])
const lastValidRecord = ref<{ timestamp: number; boxes: any[] } | null>(null)

let hasFirstFrameRendered = false
let pendingUnifiedReadyTimeout: number | null = null

function waitForFirstDetection(timeoutId: number) {
  if (pendingUnifiedReadyTimeout !== null) {
    clearTimeout(pendingUnifiedReadyTimeout)
  }
  pendingUnifiedReadyTimeout = timeoutId
}

function checkUnifiedReady() {
  if (pendingUnifiedReadyTimeout !== null && detectionBuffer.value.length > 0) {
    clearTimeout(pendingUnifiedReadyTimeout)
    pendingUnifiedReadyTimeout = null
    if (streamState.value === 'loading' || streamState.value === 'model_loading' || streamState.value === 'connecting') {
      streamState.value = 'running'
      errorMsg.value = ''
      logger.info('stream', 'Transitioning to running state from connecting/loading (Unified Ready)', { taskId: props.taskId })
      lastConfirmedPlayingTime.value = Date.now()
      const liveSyncPos = hls?.liveSyncPosition
      if (liveSyncPos && videoPlayerRef.value) {
        const diff = liveSyncPos - videoPlayerRef.value.currentTime
        if (diff > 3) {
          seekToLiveAndPlay()
        }
      }
      logger.info('system', 'Unified ready: detection data arrived, switching to running')
    }
  }
}

function seekToLiveAndPlay() {
  if (!videoPlayerRef.value || !hls) return;
  videoPlayerRef.value.play().catch(() => {});
}

// 【双策略缓存清理机制】
// 策略 1：时间窗口 - 保留最近 30 秒数据（原 60 秒过长）
// 策略 2：容量上限 - 最多 800 条记录（原 1200 条过多）
// 提前清理：在达到 80% 容量时就开始渐进式清理，避免瞬间卡顿
let pruneLogCounter = 0
let diagWsLogCounter = 0

// P0 修复：模式特定的缓存清理策略
const CACHE_TIME_WINDOW_LIVE_MS = 15000  // 实时模式：15 秒窗口
const CACHE_TIME_WINDOW_HISTORY_MS = 60000  // 历史模式：60 秒窗口（需要更多历史数据）
const CACHE_MAX_CAPACITY_LIVE = 500      // 实时模式容量
const CACHE_MAX_CAPACITY_HISTORY = 1200  // 历史模式容量
const CACHE_HARD_LIMIT_LIVE = 800       // 实时模式硬性上限
const CACHE_HARD_LIMIT_HISTORY = 1500   // 历史模式硬性上限
const CACHE_EARLY_CLEANUP_THRESHOLD_LIVE = 400  // 实时模式提前清理阈值（80%）
const CACHE_EARLY_CLEANUP_THRESHOLD_HISTORY = 960  // 历史模式提前清理阈值（80%）

function pruneDetectionBuffer() {
  const isHistoryMode = playMode.value === 'history'
  
  // 根据模式选择参数
  const timeWindowMs = isHistoryMode ? CACHE_TIME_WINDOW_HISTORY_MS : CACHE_TIME_WINDOW_LIVE_MS
  const maxCapacity = isHistoryMode ? CACHE_MAX_CAPACITY_HISTORY : CACHE_MAX_CAPACITY_LIVE
  const hardLimit = isHistoryMode ? CACHE_HARD_LIMIT_HISTORY : CACHE_HARD_LIMIT_LIVE
  const earlyCleanupThreshold = isHistoryMode ? CACHE_EARLY_CLEANUP_THRESHOLD_HISTORY : CACHE_EARLY_CLEANUP_THRESHOLD_LIVE
  
  // 策略 3：硬性上限保护
  if (detectionBuffer.value.length > hardLimit) {
    detectionBuffer.value = detectionBuffer.value.slice(-hardLimit)
    pruneLogCounter++
    if (pruneLogCounter % 20 === 0) {
      diagLogger.log('DIAG-PRUNE-HARD', JSON.stringify({
        event: 'hard_limit_cleanup',
        mode: isHistoryMode ? 'history' : 'live',
        beforeSize: detectionBuffer.value.length + (detectionBuffer.value.length - hardLimit),
        afterSize: detectionBuffer.value.length,
        hardLimit,
      }))
    }
  }
  
  if (detectionBuffer.value.length > 2) {
    const newestTs = detectionBuffer.value[detectionBuffer.value.length - 1]?.timestamp || 0
    
    // 策略 1：时间窗口清理
    const timeCutoff = newestTs - timeWindowMs
    
    // 策略 2：容量清理 - 当记录数超过提前清理阈值时，使用更激进的时间窗口
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
        clockOffset,
      }))
    }
    
    // 执行清理：移除过期数据
    // 实时模式：只保留非历史标记的数据
    // 历史模式：保留所有数据（包括历史标记）
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
    
    // 策略 2：容量清理 - 如果时间窗口清理后仍超过容量上限，强制裁剪
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

const lastDetections = ref<any[]>([])



const showOverlay = computed(() => {
  return streamState.value === 'exception';
});

const shouldShowTimeline = computed(() =>
  ['running', 'connecting', 'retry', 'loading', 'recovering', 'paused_end'].includes(streamState.value)
)

const canControl = computed(() => {
  return streamState.value !== 'exception' || playMode.value === 'history'
})

const isActuallyPlaying = computed(() => {
  // UI 按钮状态应直接反映视频底层的暂停/播放状态
  return !isVideoPaused.value;
})

const paginatedRecords = computed(() => {
  const sorted = [...records.value].sort((a, b) =>
    new Date(b.detected_at).getTime() - new Date(a.detected_at).getTime()
  )
  const start = (currentPage.value - 1) * pageSize
  return sorted.slice(start, start + pageSize)
})

onMounted(async () => {
  isActive = true
  diagLogger.setTaskId(props.taskId)
  diagLogger.log('DIAG-MOUNT', JSON.stringify({ taskId: props.taskId }))
  sendDebugLog('COMPONENT_MOUNTED')

  try {
    const task = await tasksApi.get(props.taskId)
    if (task && task.detection_config) {
      currentDetectionConfig.value = typeof task.detection_config === 'string'
        ? JSON.parse(task.detection_config)
        : task.detection_config
    }

    // Hot Start: 获取快照快速初始化
    const snapshot = await tasksApi.getSnapshot(props.taskId)
    if (snapshot) {
      if (snapshot.task) {
        const idx = taskStore.tasks.findIndex(t => t.id === props.taskId)
        if (idx >= 0) {
          taskStore.tasks[idx] = { ...taskStore.tasks[idx], ...snapshot.task }
        }
      }
      const taskStatus = snapshot.task?.status
      const lastStatus = snapshot.last_status
      const hlsReady = snapshot.hls_ready || false
      const hasHistory = snapshot.has_history || false

      // 【P2-2 修复 v2】：核心原则 - taskStatus=running 时永远走实时模式
      // 仅非 running 状态才考虑历史回放
      const isTaskRunning = taskStatus === 'running' || taskStatus === 'initializing'
      const isTerminalStatus = ['pending', 'failed', 'exception'].includes(taskStatus)

      if (isTaskRunning) {
        // ===== 实时模式（任务运行中）=====
        records.value = snapshot.recent_records || []

        if (snapshot.server_time) {
          clockOffset = snapshot.server_time - Date.now()
        }
        
        // 【三层混合校准 - 第一层】：读取后端 NTP 偏移量
        if (snapshot.ntp_offset_ms !== undefined) {
          ntpOffsetMs = snapshot.ntp_offset_ms
          ntpSynced = snapshot.ntp_synced || false
          diagLogger.log('DIAG-NTP', JSON.stringify({
            offsetMs: Math.round(ntpOffsetMs),
            avgOffsetMs: Math.round(snapshot.ntp_avg_offset_ms || 0),
            synced: ntpSynced,
          }))
        }

        const taskData = snapshot.task
        const baseSeconds = taskData?.cumulative_running_seconds || 0
        const sessionStart = taskData?.session_start_time
        if (sessionStart) {
          const sessionStartMs = new Date(sessionStart).getTime()
          sessionStartTime.value = sessionStartMs;
          firstSessionStartTime.value = sessionStartMs;
          const currentRunningSeconds = baseSeconds + (Date.now() - sessionStartMs) / 1000
          startRunningSecondsCounter(currentRunningSeconds)
        } else {
          startRunningSecondsCounter(baseSeconds)
        }

        if (hlsReady) {
          // HLS 就绪：加载实时检测框
          if (snapshot.recent_detections?.length > 0) {
            const allBoxes: any[] = [];
            const cutoffTime = Date.now() + clockOffset - 30000
            snapshot.recent_detections.forEach((msg: any) => {
              if (msg.type === 'detection') {
                const batch = msg.batch || (msg.payload ? [msg.payload] : []);
                batch.forEach((item: any) => {
                  if (item && item.boxes && item.timestamp && item.timestamp > cutoffTime) {
                    allBoxes.push({
                      timestamp: item.timestamp,
                      timestamp_ms: item.timestamp_ms || Math.round(item.timestamp * 1000),
                      boxes: item.boxes,
                      is_history: false
                    });
                  }
                });
              }
            });
            detectionBuffer.value = allBoxes;
            logger.info('system', `Hot start: Pre-loaded ${detectionBuffer.value.length} detection frames from ${snapshot.recent_detections.length} cached messages`);
          }

          const hasDetectionData = detectionBuffer.value.length > 0

          if (lastStatus && lastStatus.type === 'running' && lastStatus.hls_url) {
            if (hasDetectionData) {
              streamState.value = 'running'
            } else {
              streamState.value = 'model_loading'
              const unifiedReadyTimeout = window.setTimeout(() => {
                if (streamState.value === 'model_loading') {
                  streamState.value = 'running'
                }
                pendingUnifiedReadyTimeout = null;
              }, 30000)
              waitForFirstDetection(unifiedReadyTimeout)
            }
            if (lastStatus.server_time) clockOffset = lastStatus.server_time - Date.now()
            if (lastStatus.webrtc_url) {
              webrtcStreamPath.value = `fg_${props.taskId}_rgb`;
              switchToWebRTC();
            }
            initHls(undefined, lastStatus.hls_url)
          } else {
            streamState.value = 'model_loading'
          }
        } else {
          // HLS 未就绪（初始化中/模型加载中）：等待 WebSocket 推送 running 状态
          streamState.value = taskStatus === 'initializing' ? 'connecting' : 'model_loading'
        }
      } else if (isTerminalStatus && hasHistory) {
        // ===== 历史回放模式（任务已终止且有历史数据）=====
        skipPlayModeWatchInit = true;
        playMode.value = 'history'
        streamState.value = 'loading'

        records.value = snapshot.recent_records || []

        await loadHistory()

        // V4.4: 即使 firstSessionStartTime 未设置，也加载历史检测框
        // loadHistory() 可能无 segments，此时用 snapshot 中的时间信息作为 fallback
        const startMs = firstSessionStartTime.value
          || (snapshot.task?.session_start_time ? new Date(snapshot.task.session_start_time).getTime() : null)
          || (Date.now() - (snapshot.task?.cumulative_running_seconds || 0) * 1000)
        const endMs = Date.now()
        await loadHistoricalBoxes(startMs, endMs)

        // V4.9: 历史模式也需要加载检测事件（右侧面板）
        refreshDetectionEvents()

        if (totalDuration.value > 0 || frozenDuration.value > 0) {
          initHls()
        } else {
          streamState.value = 'exception'
          errorMsg.value = '历史视频数据不可用'
        }
      } else if (isTerminalStatus) {
        // ===== 任务已终止但无历史数据 =====
        records.value = snapshot.recent_records || []
        detectionBuffer.value = []
        streamState.value = 'exception'
        errorMsg.value = '任务已结束，暂无历史视频数据'
      } else {
        // ===== 其他未知状态 =====
        records.value = snapshot.recent_records || []
        detectionBuffer.value = []
        streamState.value = 'exception'
        errorMsg.value = '暂无可用视频数据'
      }
    }

    if (snapshot?.task?.status === 'running' && playMode.value !== 'history') {
      connect()
      if (!runningSecondsTimer) {
        const taskData = snapshot.task
        const baseSeconds = taskData?.cumulative_running_seconds || 0
        const sessionStart = taskData?.session_start_time
        if (sessionStart) {
          const sessionStartMs = new Date(sessionStart).getTime()
          startRunningSecondsCounter(baseSeconds + (Date.now() - sessionStartMs) / 1000)
        } else {
          startRunningSecondsCounter(baseSeconds)
        }
      }
    }
    
    // 确保 renderLoop 在检测数据先于视频到达时也能启动
    if (detectionBuffer.value.length > 0 && !rafId) {
      renderLoop();
    }
    
  } catch (e) {
    logger.error('system', 'Failed to perform Hot Start', { error: String(e) })
    // 回退：尝试直接连接
    if (playMode.value !== 'history') {
      connect()
    }
  }

  if (videoPlayerRef.value) {
    resizeObserver = new ResizeObserver(() => {})
    resizeObserver.observe(videoPlayerRef.value)
  }
  
  // 兜底缓存清理：每 5 秒执行，确保 renderLoop 停止时也能清理
  if (bufferCleanupTimer) clearInterval(bufferCleanupTimer)
  bufferCleanupTimer = window.setInterval(() => {
    pruneDetectionBuffer()
  }, 5000)
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

    if (pendingUnifiedReadyTimeout !== null) {
      clearTimeout(pendingUnifiedReadyTimeout)
      pendingUnifiedReadyTimeout = null
    }

    forceDisconnect()

    destroyPlayer()
    
    if (playbackMode.value === 'webrtc') {
      webrtcPlayerRef.value?.stopWebRTC();
      playbackMode.value = 'hls';
    }
    
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

    if (clockCalibrationInterval) {
      clearInterval(clockCalibrationInterval)
      clockCalibrationInterval = null
    }
    
    if (bufferCleanupTimer) {
      clearInterval(bufferCleanupTimer)
      bufferCleanupTimer = null
    }

    stopEventsPolling()

    cancelStaleReconnect();
    stopLiveTimeCounter();
    stopRunningSecondsCounter();
  } catch (e) {
    logger.error('system', 'Component unmount failure', { error: String(e) })
  }
})

let skipPlayModeWatchInit = false

watch(playMode, (newMode) => {
  if (newMode === 'history') {
    tasksApi.getDetectionRecords(props.taskId, { limit: 1000 }).then(res => {
      records.value = res.records || []
    }).catch(e => logger.warn('api', 'Failed to load detection records', { error: String(e) }))

    if (!skipPlayModeWatchInit) {
      loadHistory()
    }
    skipPlayModeWatchInit = false
  }
}, { immediate: false })

watch(() => props.taskId, (newId) => {
  if (newId) {
    forceDisconnect()
    isFrozen.value = false
    playMode.value = 'live'
    destroyPlayer()
    connect()
    loadHistory()
    initPlayer()
  }
})

watch(() => currentTask.value?.status, (status) => {
  // V5.3: 扩展重连条件，增加 WebSocket 断开连接的判断
  // 当任务重新运行时，如果 streamState 不在 running 状态或 WebSocket 未连接，则重连
  const shouldReconnect = status === 'running' && (
    streamState.value === 'paused_end' ||
    streamState.value === 'exception' ||
    !ws || ws.readyState !== WebSocket.OPEN
  )
  if (shouldReconnect) {
    destroyPlayer()
    forceDisconnect()
    detectionBuffer.value = [];
    lastValidRecord.value = null;
    pendingRecords.value = [];
    // 不清空 records.value — 续存模式下保留历史记录
    connect()
    initPlayer()
  }
})

function connect() {
  const token = localStorage.getItem('token')
  isActive = true

  if (streamState.value !== 'running') {
    streamState.value = 'connecting'
  }
  errorMsg.value = ''
  retryAttempt.value = 0
  wsReconnectCount.value = 0
  pendingRecords.value = []

  // If we stay in 'connecting' for too long (e.g. 15s), show an error instead of hanging with a gray mask
  if (connectionTimeout) clearTimeout(connectionTimeout)
  connectionTimeout = setTimeout(() => {
    if (streamState.value === 'connecting' && isActive) {
      logger.warn('websocket', 'Initial connection timed out', { taskId: props.taskId })
      streamState.value = 'exception'
      errorMsg.value = '连接推理引擎超时，请尝试刷新页面或重新执行任务'
    }
  }, 15000)

    logger.info('websocket', 'Attempting to connect', { taskId: props.taskId, url: `${props.wsUrl}/${props.taskId}` });
    ws = new WebSocket(`${props.wsUrl}/${props.taskId}?token=${token}`)

    ws.onopen = () => {
      logger.info('websocket', 'Connected', { taskId: props.taskId });
      // V3.0: 启动事件驱动检测记录轮询
      startEventsPolling()

      // 【P4 修复】：启动 clockOffset 定期校准，每 30 秒校准一次
      if (clockCalibrationInterval) clearInterval(clockCalibrationInterval)
      clockCalibrationInterval = window.setInterval(async () => {
        if (!isActive || !ws || ws.readyState !== WebSocket.OPEN) {
          if (clockCalibrationInterval) {
            clearInterval(clockCalibrationInterval)
            clockCalibrationInterval = null
          }
          return
        }
        try {
          const snapshot = await tasksApi.getSnapshot(props.taskId)
          if (snapshot?.server_time) {
            const newOffset = snapshot.server_time - Date.now()
            const drift = Math.abs(newOffset - clockOffset)
            if (drift > 500) {
              logger.info('websocket', `Clock offset recalibrated: drift=${drift}ms, new=${newOffset}ms`)
            }
            clockOffset = newOffset
          }
        } catch {
          // 静默忽略校准失败
        }
      }, 30000)
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
          logger.debug('websocket', `Received message type: ${data.type}`, { taskId: props.taskId, dataType: data.type });
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
        case 'time_sync':
          if (data.server_time_ms && data.video_pts_ms) {
            const t1 = Date.now();
            const serverTime = data.server_time_ms;
            const videoPts = data.video_pts_ms;
            
            // 计算时钟偏移（简化版NTP算法）
            const newOffset = serverTime - t1;
            
            // 使用指数移动平均平滑偏移量
            const prevOffset = clockOffset;
            clockOffset = Math.round(prevOffset * 0.9 + newOffset * 0.1);
            
            // 记录诊断日志
            if (Math.abs(clockOffset - prevOffset) > 10) {
              diagLogger.log('DIAG-TIME-SYNC', JSON.stringify({
                serverTime,
                clientTime: t1,
                videoPts,
                rawOffset: newOffset,
                smoothedOffset: clockOffset,
                drift: Math.abs(clockOffset - prevOffset),
              }));
            }
          }
          break;
        case 'connecting':
        case 'rtsp_connecting':
          streamState.value = 'connecting'
          if (data.message) errorMsg.value = data.message
          break
        case 'loading':
          streamState.value = 'loading'
          if (data.message) errorMsg.value = data.message
          break
        case 'model_loading':
          if (Date.now() - lastRecoveredTime < RECOVERED_DEBOUNCE_MS) {
            break;
          }
          streamState.value = 'model_loading'
          if (data.message) errorMsg.value = data.message
          break
        case 'retry':
          streamState.value = 'retry'
          retryAttempt.value = data.attempt || 0
          retryMax.value = data.max || 5
          break
        case 'recovered':
          lastRecoveredTime = Date.now()
          streamState.value = 'recovering'
          break
        case 'running':
          if (data.server_time) {
            clockOffset = data.server_time - Date.now();
          }
          errorMsg.value = ''
          if (!runningSecondsTimer) {
            const baseSeconds = data.cumulative_running_seconds ?? currentTask.value?.cumulative_running_seconds ?? 0
            const sessionStart = data.session_start_time ?? currentTask.value?.session_start_time
            if (sessionStart) {
              const sessionStartMs = new Date(sessionStart).getTime()
              const currentRunningSeconds = baseSeconds + (Date.now() - sessionStartMs) / 1000
              startRunningSecondsCounter(currentRunningSeconds)
            } else {
              startRunningSecondsCounter(baseSeconds)
            }
          }
          if (!hls && data.hls_url) {
            initHls(undefined, data.hls_url); 
          }
          if (data.webrtc_url && playbackMode.value === 'hls') {
            webrtcStreamPath.value = `fg_${props.taskId}_rgb`;
            switchToWebRTC();
          }
          if (detectionBuffer.value.length > 0) {
            streamState.value = 'running'
          } else {
            streamState.value = 'model_loading'
            const unifiedReadyTimeout = window.setTimeout(() => {
              if (streamState.value === 'model_loading') {
                streamState.value = 'running'
              }
              pendingUnifiedReadyTimeout = null;
            }, 30000)
            waitForFirstDetection(unifiedReadyTimeout)
          }
          break;
        case 'error':
          streamState.value = 'exception'
          errorMsg.value = data.message || '视频连接发生未知错误'
          break
        case 'snapshot':
          if (data.records) records.value = data.records
          break
        case 'record_event':
          diagLogger.log('DIAG-RE', JSON.stringify({ event: 'received', count: (data.records || []).length, playMode: playMode.value, recordsLen: records.value.length, pendingLen: pendingRecords.value.length }));
          const newRecords = data.records || (data.record ? [data.record] : []);
          if (newRecords.length > 0) {
            // 【双时间戳架构】：为每条记录添加 timestamp_ms 用于五位一体同步
            const sorted = [...newRecords].sort((a: any, b: any) => {
              const tsA = a.timestamp_ms || new Date(a.detected_at).getTime();
              const tsB = b.timestamp_ms || new Date(b.detected_at).getTime();
              return tsA - tsB;
            });
            // 无论 live 还是 history，统一将记录写入待释放队列进行绝对时间戳物理同步
            sorted.forEach((r: any) => {
              if (!r.timestamp_ms) {
                r.timestamp_ms = new Date(r.detected_at).getTime();
              }
              pendingRecords.value.push(r);
            });
            if (pendingRecords.value.length > 2000) {
              pendingRecords.value.splice(0, pendingRecords.value.length - 2000);
            }
            diagLogger.log('DIAG-RE', JSON.stringify({ event: 'pushed_to_pending', newLen: pendingRecords.value.length }));
          }
          break
        case 'detection':
          {
            const items = data.batch || (data.payload ? [data.payload] : []);
            lastDetectionMessageTime = Date.now();
            
            if (playMode.value === 'history') {
              // 历史模式下实时检测数据也进入 detectionBuffer（标记为 history），避免 buffer 清空后无数据
              items.forEach((item: any) => {
                if (item && item.boxes) {
                  detectionBuffer.value.push({
                    timestamp: item.timestamp,
                    timestamp_ms: item.timestamp_ms || Math.round(item.timestamp * 1000),
                    wall_clock: item.wall_clock || (Date.now() / 1000),
                    boxes: item.boxes,
                    is_history: true,
                  });
                  liveDetectionCache.push({
                    timestamp: item.timestamp,
                    boxes: item.boxes,
                  });
                }
              });
              if (liveDetectionCache.length > 300) {
                liveDetectionCache.splice(0, liveDetectionCache.length - 300);
              }
              pruneDetectionBuffer();
              return;
            }

            let addedCount = 0;
            items.forEach((item: any) => {
              if (item && item.boxes) {
                detectionBuffer.value.push({
                  timestamp: item.timestamp,
                  timestamp_ms: item.timestamp_ms || Math.round(item.timestamp * 1000),
                  wall_clock: item.wall_clock || (Date.now() / 1000),
                  boxes: item.boxes,
                  is_history: item.is_history ?? false,
                });
                addedCount++;
              }
            });
            
            if (diagWsLogCounter % 50 === 0) {
              diagLogger.log('DIAG-WS-DETECT', JSON.stringify({
                receivedItems: items.length,
                addedToBuffer: addedCount,
                bufferSizeAfter: detectionBuffer.value.length,
                firstTs: items[0]?.timestamp,
                lastTs: items[items.length - 1]?.timestamp,
              }));
            }
            diagWsLogCounter++;
            
            pruneDetectionBuffer();
            checkUnifiedReady();
            if (pendingRecords.value.length > 2000) {
              pendingRecords.value.splice(0, pendingRecords.value.length - 2000);
            }
          }
          break
        case 'status':
          if (data.status === 'exception' || data.status === 'error') {
            streamState.value = 'exception'
            errorMsg.value = data.message || ''
            emit('status-change', 'exception')
          } else if (data.status === 'pending') {
            // 任务重启后 WebSocket 重连的关键触发条件
            streamState.value = 'paused_end'
            emit('status-change', 'pending')
          } else if (data.status === 'deleted') {
            terminatingTasks.add(props.taskId)
            message.warning('当前任务已被移除')
            emit('close')
          }
          break
      }
    } catch (e) {
      logger.error('websocket', 'Failed to parse message', { taskId: props.taskId, error: String(e) })
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

    // code 1006/1011: 后端重启，延长重连间隔避免请求堆积
    const isBackendRestart = event.code === 1006 || event.code === 1011

    if (wsReconnectCount.value < WS_MAX_RECONNECT) {
      wsReconnectCount.value++
      streamState.value = 'recovering'
      const baseDelay = isBackendRestart ? 3000 : 1000
      const delay = Math.min(baseDelay * wsReconnectCount.value, 15000)

      setTimeout(() => {
        if (isActive) {
          connect()
        }
      }, delay)
    } else {
      if (isBackendRestart) {
        streamState.value = 'exception'
        errorMsg.value = '后端服务已重启，请刷新页面重新连接'
        emit('status-change', 'exception')
        return
      }
      tasksApi.get(props.taskId).then(task => {
        if (task.status === 'running') {
          streamState.value = 'exception'
          errorMsg.value = '视频流连接持续不稳定，请检查网络后刷新页面'
          emit('status-change', 'exception')
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
    logger.error('websocket', 'WebSocket error', { taskId: props.taskId })
    loadHistory()
  }
}

async function initHls(seekToSeconds?: number, overrideUrl?: string) {
  if (!videoPlayerRef.value || !isActive) return;

  let streamUrl = overrideUrl || '';
  if (!streamUrl) {
    if (playMode.value === 'live') {
      streamUrl = `/storage/${props.taskId}/stream_${currentChannel.value}.m3u8`;
    } else {
      const endTs = frozenDuration.value || totalDuration.value;
      streamUrl = `/api/tasks/${props.taskId}/vod-stream?channel=${currentChannel.value}&end_time=${endTs}`;
    }
  }

  streamState.value = playMode.value === 'history' ? 'loading' : 'connecting';

  if (Hls.isSupported()) {
    if (hls && hls.media === videoPlayerRef.value) {
      pendingSeekSeconds = seekToSeconds;
      hls.loadSource(streamUrl);
      setTimeout(() => {
        if (isSwitchingStream) {
          logger.warn('playback', 'isSwitchingStream timeout (hot-switch), forcing reset', { taskId: props.taskId });
          isSwitchingStream = false;
          lastValidRecord.value = null;
        }
      }, 1500);
    } else {
      destroyPlayer();
      pendingSeekSeconds = seekToSeconds;

      hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 10,
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
        liveSyncDurationCount: 5.5,
        liveMaxLatencyDurationCount: 6.0,
        highBufferWatchdogPeriod: 10,
        manifestLoadingMaxRetry: 10,
        manifestLoadingTimeOut: 5000,
        manifestLoadingRetryDelay: 1000,
        liveDurationInfinity: true,
        startLevel: -1,
        testBandwidth: false,
        progressive: true,
      });

      hls.loadSource(streamUrl);
      hls.attachMedia(videoPlayerRef.value);

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        sendDebugLog('HLS_MANIFEST_PARSED');
        if (!videoPlayerRef.value) return;

        const taskStatus = currentTask.value?.status;
        
        if (playMode.value === 'live') {
          // 仅当 taskStatus 和 currentTask.status 双重确认为终态时才切历史模式
          const terminalStatuses = ['pending', 'failed', 'exception'];
          const confirmedTerminal = taskStatus && terminalStatuses.includes(taskStatus)
            && currentTask.value?.status && terminalStatuses.includes(currentTask.value.status);
          if (confirmedTerminal) {
            skipPlayModeWatchInit = true;
            playMode.value = 'history';
            frozenDuration.value = hls?.levels[0]?.details?.totalduration || videoPlayerRef.value.duration;
            videoPlayerRef.value.currentTime = 0;
            videoPlayerRef.value.play().catch(() => {});
            loadHistory();
            fetchHistoricalRecords(0, frozenDuration.value);
          } else {
            seekToLiveAndPlay();
            streamState.value = 'running';
          }
          lastValidRecord.value = null;
        } else {
          streamState.value = 'running';
          
          const effectiveSeek = pendingSeekSeconds;
          pendingSeekSeconds = undefined;
          
          if (effectiveSeek !== undefined && effectiveSeek > 0) {
            videoPlayerRef.value.pause();
            const performSeek = () => {
              if (!videoPlayerRef.value || !isActive) return;
              if (videoPlayerRef.value.duration && isFinite(videoPlayerRef.value.duration)) {
                videoPlayerRef.value.currentTime = effectiveSeek;
                videoPlayerRef.value.play().catch(() => {});
                hls?.off(Hls.Events.FRAG_LOADED, performSeek);
              }
            };
            if (videoPlayerRef.value.duration && isFinite(videoPlayerRef.value.duration)) {
              videoPlayerRef.value.currentTime = effectiveSeek;
              videoPlayerRef.value.play().catch(() => {});
            } else {
              hls?.on(Hls.Events.FRAG_LOADED, performSeek);
            }
          } else {
            videoPlayerRef.value.play().catch(() => {});
          }
          lastValidRecord.value = null;
        }
        
        setTimeout(() => {
          if (isActive && streamState.value === 'running' && !isVideoActuallyPlaying.value && videoPlayerRef.value) {
            const v = videoPlayerRef.value;
            if (v.readyState < 3) {
              logger.warn('stream', 'First frame timeout, forcing seek to live edge');
              const newPos = (hls?.liveSyncPosition || 0) - 1;
              v.currentTime = Math.max(0, newPos);
              v.play().catch(() => {});
            }
          }
        }, 8000);  // 8s 超时：给予后端 HLS 管道充足的冷启动时间

        if (playMode.value === 'live') {
          setTimeout(() => {
            if (isActive && hls?.liveSyncPosition && videoPlayerRef.value) {
              const diff = hls.liveSyncPosition - videoPlayerRef.value.currentTime;
              if (diff > 5) {
                logger.info('stream', 'Late seekToLive: correcting large drift', { diff: diff.toFixed(1) });
                seekToLiveAndPlay();
              }
            }
          }, 2000);
        }
      });

      setTimeout(() => {
        if (isSwitchingStream) {
          logger.warn('playback', 'isSwitchingStream timeout, forcing reset', { taskId: props.taskId });
          isSwitchingStream = false;
          lastValidRecord.value = null;
        }
      }, 1500);

      hls.on(Hls.Events.FRAG_CHANGED, (event, data) => {
        if (data.frag.programDateTime) {
          currentFragProgramDateTime.value = data.frag.programDateTime;
          currentFragVideoTime.value = data.frag.start;
          
          if (firstSessionStartTime.value === null) {
            firstSessionStartTime.value = data.frag.programDateTime - (data.frag.start * 1000);
            sessionStartTime.value = firstSessionStartTime.value;
          }
        }
      });

      let manifest404RetryCount = 0;
      const MAX_MANIFEST_404_RETRIES = 10;
      
      let frag404RetryCount = 0;
      const MAX_FRAG_404_RETRIES = 10;

      hls.on(Hls.Events.ERROR, (event, data) => {
        if (data.fatal) {
          switch (data.type) {
            case Hls.ErrorTypes.NETWORK_ERROR:
              logger.warn('hls', 'Network error', { taskId: props.taskId, details: data.details });
              if (data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR && data.response?.code === 404) {
                manifest404RetryCount++;
                if (manifest404RetryCount > MAX_MANIFEST_404_RETRIES) {
                  logger.error('hls', 'Manifest 404 max retries exceeded', { taskId: props.taskId, retries: manifest404RetryCount });
                  streamState.value = 'exception';
                  errorMsg.value = '视频流初始化失败，请检查后端服务或重新执行任务';
                  destroyPlayer();
                  return;
                }
                logger.info('hls', `Manifest 404, retry ${manifest404RetryCount}/${MAX_MANIFEST_404_RETRIES} in 1s`, { taskId: props.taskId });
                if (streamState.value !== 'loading' && streamState.value !== 'model_loading') {
                  streamState.value = 'loading';
                }
                setTimeout(() => {
                  if (isActive && hls) {
                    hls.loadSource(streamUrl);
                  }
                }, 1000);
              } else if (data.details === Hls.ErrorDetails.FRAG_LOAD_ERROR && data.response?.code === 404) {
                frag404RetryCount++;
                if (frag404RetryCount > MAX_FRAG_404_RETRIES) {
                  logger.error('hls', 'Fragment 404 max retries exceeded, stream is dead', { taskId: props.taskId });
                  streamState.value = 'exception';
                  errorMsg.value = '视频推流已意外中断，请检查摄像头或视频源状态';
                  destroyPlayer();
                  return;
                }
                logger.info('hls', `Fragment 404, retry ${frag404RetryCount}/${MAX_FRAG_404_RETRIES}`, { taskId: props.taskId });
                if (streamState.value !== 'buffering') streamState.value = 'buffering';
                setTimeout(() => {
                  if (isActive && hls) hls.startLoad();
                }, 1000);
              } else {
                hls?.startLoad();
              }
              break;
            case Hls.ErrorTypes.MEDIA_ERROR:
              logger.error('hls', 'Media error', { taskId: props.taskId, details: data.details });
              hls?.recoverMediaError();
              break;
            default:
              destroyPlayer();
              break;
          }
        } else {
          if (data.details === Hls.ErrorDetails.FRAG_LOAD_ERROR) {
            logger.debug('hls', 'Non-fatal fragment load error', { taskId: props.taskId });
          }
        }
      });
    }
  } else if (videoPlayerRef.value.canPlayType('application/vnd.apple.mpegurl')) {
    videoPlayerRef.value.src = streamUrl;
    videoPlayerRef.value.play().catch(() => {});
  }
}

const initPlayer = initHls;

function destroyPlayer() {
  if (hls) {
    hls.destroy();
    hls = null;
  }
  if (playbackMode.value === 'webrtc') {
    webrtcPlayerRef.value?.stopWebRTC();
  }
  isVideoActuallyPlaying.value = false;
  isDetectionStale.value = false;
  lastVideoAbsTime = 0;
  lastVideoAbsTimeTs = 0;
  timeCalibrationMs = 0;
  calibrationInitialized = false;
  hasFirstFrameRendered = false;
  cancelStaleReconnect();
  stopRunningSecondsCounter();
}

function scheduleStaleReconnect() {
  if (playMode.value === 'history') return;
  if (detectionStaleReconnectTimer !== null) return;
  detectionStaleReconnectTimer = window.setTimeout(async () => {
    if (!isDetectionStale.value) {
      detectionStaleReconnectTimer = null;
      return;
    }
    diagLogger.log('DIAG-STALE', JSON.stringify({ event: 'reconnect_attempt', wsState: ws?.readyState, bufferSize: detectionBuffer.value.length }));
    logger.info('system', 'Detection data stale, attempting recovery...');
    try {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        logger.info('system', 'WebSocket not open, reconnecting...');
        connect();
      } else {
        await refreshRecords();
        logger.info('system', 'WebSocket still open, requesting detection restart');
        ws.send(JSON.stringify({ type: 'restart_detection' }));
      }
    } finally {
      detectionStaleReconnectTimer = null;
    }
  }, STALE_RECONNECT_THRESHOLD_MS);
}

function cancelStaleReconnect() {
  if (detectionStaleReconnectTimer !== null) {
    clearTimeout(detectionStaleReconnectTimer);
    detectionStaleReconnectTimer = null;
  }
}

async function refreshRecords() {
  loadingRecords.value = true
  try {
    const res = await tasksApi.getDetectionRecords(props.taskId, { limit: 100 })
    records.value = res.records
  } catch (e) {
    logger.error('api', 'Failed to refresh records', { taskId: props.taskId, error: String(e) })
  } finally {
    loadingRecords.value = false
  }
}

async function refreshDetectionEvents() {
  try {
    const [eventsRes, summaryRes] = await Promise.all([
      tasksApi.getDetectionEvents(props.taskId, { limit: 100, order: 'desc' }),
      tasksApi.getDetectionEventsSummary(props.taskId),
    ])
    detectionEvents.value = eventsRes.events
    activeTracks.value = summaryRes.total_targets
  } catch (e) {
    diagLogger.warn('events-poll', `Failed to refresh detection events: ${e}`, { taskId: props.taskId })
  }
}

function startEventsPolling() {
  stopEventsPolling()
  refreshDetectionEvents()
  eventsPollingTimer = window.setInterval(() => {
    refreshDetectionEvents()
  }, 2000)
}

function stopEventsPolling() {
  if (eventsPollingTimer) {
    clearInterval(eventsPollingTimer)
    eventsPollingTimer = null
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
      if (playMode.value === 'history' && estimatedDuration > totalDuration.value) {
        totalDuration.value = estimatedDuration
      }

      if (merged.first_session_start_time) {
        firstSessionStartTime.value = new Date(merged.first_session_start_time).getTime();
        sessionStartTime.value = firstSessionStartTime.value;
      }

      if (merged.session_map && merged.session_map.length > 0) {
        sessionMap.value = merged.session_map;
      }

      if (playMode.value !== 'history' && records.value.length === 0) {
        try {
          const recordsRes = await tasksApi.getDetectionRecords(props.taskId, { limit: 1000 })
          records.value = recordsRes.records || []
        } catch (e) {
          logger.warn('api', 'Detection records failed to load', { taskId: props.taskId, error: String(e) })
        }
      }
    }
    sendDebugLog('LOAD_HISTORY_SUCCESS', { count: res.segments?.length })
  } catch (e: any) {
    if (axios.isCancel(e) || e.name === 'AbortError' || e.message === 'canceled') return
    logger.error('api', 'History failed to load', { taskId: props.taskId, error: e.message })
    sendDebugLog('LOAD_HISTORY_ERROR', { error: e.message })
  } finally {
    loadingHistory.value = false
  }
}

// fetchTaskStartTime is deprecated as we use programDateTime

// ========== Phase 3: 核心渲染引擎 (requestAnimationFrame) ==========
// 放弃依赖 WebSocket 的触发，改为根据视频当前的物理进度主动去缓冲池里"捞"框
function renderLoop() {
  if (!isActive) {
    return;
  }

  // 1. 优先获取最核心的物理绝对时间并对外广播以实现对齐，防止因 early returns (如 webrtc, lagging, switching) 漏发
  const videoElement = videoPlayerRef.value || null;
  const currentVideoAbsTime = getVideoAbsTime(videoElement);
  const calibratedVideoTime = currentVideoAbsTime - timeCalibrationMs;
  const baseCalibratedTime = playbackMode.value === 'hls'
    ? currentVideoAbsTime
    : (calibrationInitialized
        ? calibratedVideoTime
        : currentVideoAbsTime - (cachedBufferDelayMs > 0 ? cachedBufferDelayMs : 3000));

  // 历史模式下引入 -2500ms 的反向时间对齐补偿，校正 AI 推理、网络传输及分片打包带来的物理时滞
  // 实时模式下不补偿：HLS 本身已有 5-6 秒延迟，再补偿会导致 effectiveTime 过度滞后于检测记录时间戳，
  // 使得 pendingRecords 中的记录因 recordTime > effectiveCalibratedTime 而永远无法释放
  const compensationMs = playMode.value === 'history' ? -2500 : 0;
  const effectiveCalibratedTime = baseCalibratedTime + compensationMs;

  if (Math.abs(effectiveCalibratedTime - lastEmittedAbsoluteTime) >= 100) {
    emit('playback-absolute-time', effectiveCalibratedTime);
    lastEmittedAbsoluteTime = effectiveCalibratedTime;
  }

  const isPaused = isVideoPaused.value;
  const shouldUpdateRecords = playMode.value === 'live' || !isPaused;

  if (playbackMode.value === 'webrtc') {

    if (shouldUpdateRecords && pendingRecords.value.length > 0) {
      const now = Date.now()
      if (now - recordsReleaseSecondStart >= 1000) {
        recordsReleasedThisSecond = 0
        recordsReleaseSecondStart = now
      }
      let movedCount = 0;
      const MAX_RECORDS_PER_FRAME = 10;
      while (pendingRecords.value.length > 0 && movedCount < MAX_RECORDS_PER_FRAME && recordsReleasedThisSecond < MAX_RECORDS_PER_SECOND) {
        const first = pendingRecords.value[0];
        const recordTime = new Date(first.detected_at).getTime();
        if (recordTime <= effectiveCalibratedTime || (effectiveCalibratedTime - recordTime) > FORCE_RELEASE_THRESHOLD_MS) {
          const record = pendingRecords.value.shift();
          if (record) {
            records.value.unshift(record);
            movedCount++;
            recordsReleasedThisSecond++;
          }
        } else {
          break;
        }
      }
      if (movedCount > 0 && records.value.length > 1000) {
        records.value = records.value.slice(0, 1000);
      }
    }

    let closestRecord: { timestamp: number; boxes: any[]; timestamp_ms?: number } | null = null;
    let minDiff = Infinity;
    const SYNC_WINDOW_MS = 5000;

    if (calibrationInitialized) {
      for (const record of detectionBuffer.value) {
        const recordTimestamp = record.timestamp_ms || record.timestamp;
        const timeDiff = calibratedVideoTime - recordTimestamp;
        if (timeDiff >= 0 && timeDiff < SYNC_WINDOW_MS && timeDiff < minDiff) {
          minDiff = timeDiff;
          closestRecord = record;
        }
      }
    } else if (detectionBuffer.value.length > 0) {
      const degradedCalibMs = cachedBufferDelayMs > 0 ? cachedBufferDelayMs : 6000;
      const degradedVideoTime = currentVideoAbsTime - degradedCalibMs;
      const DEGRADED_SYNC_WINDOW_MS = 15000;

      for (let i = detectionBuffer.value.length - 1; i >= 0; i--) {
        const record = detectionBuffer.value[i];
        const recordTimestamp = record.timestamp_ms || record.timestamp;
        const timeDiff = degradedVideoTime - recordTimestamp;
        if (timeDiff >= 0 && timeDiff < DEGRADED_SYNC_WINDOW_MS) {
          closestRecord = record;
          minDiff = timeDiff;
          break;
        }
      }
    }

    if (!closestRecord && detectionBuffer.value.length > 0 && playMode.value === 'live') {
      const lastRecord = detectionBuffer.value[detectionBuffer.value.length - 1];
      const timeSinceLastDetectionMs = Date.now() - lastDetectionMessageTime;
      if (timeSinceLastDetectionMs < 5000 && lastRecord) {
        closestRecord = lastRecord;
      }
    }

    if (closestRecord && closestRecord.boxes.length > 0) {
      lastValidRecord.value = { timestamp: closestRecord.timestamp, boxes: closestRecord.boxes };
    } else if (closestRecord && closestRecord.boxes.length === 0) {
      lastValidRecord.value = null;
    }

    rafId = requestAnimationFrame(renderLoop);
    return;
  }

  if (!videoPlayerRef.value) {
    if (isActive) rafId = requestAnimationFrame(renderLoop);
    return;
  }

  if (isSwitchingStream) {
    rafId = requestAnimationFrame(renderLoop);
    return;
  }

  const video = videoPlayerRef.value;

  if (!hasFirstFrameRendered) {
    if (video.readyState >= 3) {
      hasFirstFrameRendered = true;
    } else {
      rafId = requestAnimationFrame(renderLoop);
      return;
    }
  }

  const isLagging = !isVideoActuallyPlaying.value && !isPaused && !isSwitchingStream;

  if (isLagging) {
    // 【P0-4 修复】：lastValidRecord 超时清除机制，防止"幽灵检测框"
    const nowMs = Date.now()
    if (lastValidRecord.value && (nowMs - lastValidRecordUpdateTime) > LAST_VALID_RECORD_TIMEOUT_MS) {
      // 超过 5 秒没有新检测框，清除幽灵框
      diagLogger.log('DIAG-GHOST-BOX', JSON.stringify({
        event: 'timeout_clear',
        age: nowMs - lastValidRecordUpdateTime,
        boxesCount: lastValidRecord.value.boxes.length,
      }))
      lastValidRecord.value = null
    }
    
    if (lastValidRecord.value && lastValidRecord.value.boxes.length > 0) {
      // V4.6: 内嵌方案 — 检测框由 AnnotatedHLSWriter 嵌入 HLS 视频流，前端不做 Canvas 绘制
    } else if (detectionBuffer.value.length > 0 && playMode.value === 'live') {
      const lastBuf = detectionBuffer.value[detectionBuffer.value.length - 1];
      if (lastBuf && lastBuf.boxes.length > 0) {
        lastValidRecord.value = { timestamp: lastBuf.timestamp, boxes: lastBuf.boxes };
        lastValidRecordUpdateTime = nowMs
      }
    }
    
    // 【P0-4 诊断日志】：卡顿态记录释放
    if (shouldUpdateRecords && pendingRecords.value.length > 0) {
      const now = Date.now()
      if (now - recordsReleaseSecondStart >= 1000) {
        recordsReleasedThisSecond = 0
        recordsReleaseSecondStart = now
      }
      let movedCount = 0;
      const MAX_RECORDS_PER_FRAME = 10;
      while (pendingRecords.value.length > 0 && movedCount < MAX_RECORDS_PER_FRAME && recordsReleasedThisSecond < MAX_RECORDS_PER_SECOND) {
        const first = pendingRecords.value[0];
        const recordTime = new Date(first.detected_at).getTime();
        if (recordTime <= effectiveCalibratedTime || (effectiveCalibratedTime - recordTime) > FORCE_RELEASE_THRESHOLD_MS) {
          const record = pendingRecords.value.shift();
          if (record) {
            records.value.unshift(record);
            movedCount++;
            recordsReleasedThisSecond++;
          }
        } else {
          break;
        }
      }
      if (movedCount > 0 && records.value.length > 1000) {
      records.value = records.value.slice(0, 1000);
    }
    if (Date.now() - lastRecordsReleaseDiag > 5000) {
      diagLogger.log('DIAG-RECORDS-RELEASE', JSON.stringify({
        movedCount,
        pendingRemaining: pendingRecords.value.length,
        recordsTotal: records.value.length,
        effectiveTime: Math.round(effectiveCalibratedTime),
        firstPendingTime: pendingRecords.value[0] ? new Date(pendingRecords.value[0].detected_at).getTime() : null
      }))
      lastRecordsReleaseDiag = Date.now()
    }
  }
  rafId = requestAnimationFrame(renderLoop);
  return;
}



  if (shouldUpdateRecords && pendingRecords.value.length > 0) {
    const now = Date.now()
    if (now - recordsReleaseSecondStart >= 1000) {
      recordsReleasedThisSecond = 0
      recordsReleaseSecondStart = now
    }
    let movedCount = 0;
    const MAX_RECORDS_PER_FRAME = 10;
    while (pendingRecords.value.length > 0 && movedCount < MAX_RECORDS_PER_FRAME && recordsReleasedThisSecond < MAX_RECORDS_PER_SECOND) {
      const first = pendingRecords.value[0];
      // 【双时间戳架构】：使用 timestamp_ms 进行五位一体同步
      const recordTime = first.timestamp_ms || new Date(first.detected_at).getTime();
      if (recordTime <= effectiveCalibratedTime || (effectiveCalibratedTime - recordTime) > FORCE_RELEASE_THRESHOLD_MS) {
        const record = pendingRecords.value.shift();
        if (record) {
          records.value.unshift(record);
          movedCount++;
          recordsReleasedThisSecond++;
        }
      } else {
        break;
      }
    }
    if (movedCount > 0 && records.value.length > 1000) {
      records.value = records.value.slice(0, 1000);
    }
    if (Date.now() - lastRecordsReleaseDiag > 5000) {
      diagLogger.log('DIAG-RECORDS-RELEASE', JSON.stringify({
        movedCount,
        pendingRemaining: pendingRecords.value.length,
        recordsTotal: records.value.length,
        effectiveTime: Math.round(effectiveCalibratedTime),
        firstPendingTime: pendingRecords.value[0] ? new Date(pendingRecords.value[0].detected_at).getTime() : null
      }))
      lastRecordsReleaseDiag = Date.now()
    }
  }

  const lastRenderedTime = (window as any)._lastRenderedTime || 0;
  if (isPaused && !isUserSeeking.value && Math.abs(video.currentTime - lastRenderedTime) < 0.05) {
    // 暂停态且没有拖拽：直接跳过绘图更新，保留上一帧内容
    rafId = requestAnimationFrame(renderLoop);
    return;
  }
  (window as any)._lastRenderedTime = video.currentTime;

  if (detectionBuffer.value.length > 0) {
    let bestCalibSample = 0;
    let bestCalibAbsDiff = Infinity;
    for (const record of detectionBuffer.value) {
      const recordTimestamp = record.timestamp_ms || record.timestamp;
      const signed = currentVideoAbsTime - recordTimestamp;
      if (Math.abs(signed) <= CALIBRATION_CLAMP_MS) {
        if (Math.abs(signed) < bestCalibAbsDiff) {
          bestCalibAbsDiff = Math.abs(signed);
          bestCalibSample = signed;
        }
      }
    }
    if (bestCalibAbsDiff < Infinity) {
      // 【NTP 式动态校准】：异常值过滤
      if (isOutlier(bestCalibSample)) {
        if (calibrationSampleCount % 50 === 0) {
          diagLogger.log('DIAG-CALIB', JSON.stringify({
            event: 'outlier_filtered',
            sample: Math.round(bestCalibSample),
            historySize: calibrationHistory.length,
          }));
        }
      } else {
        addCalibrationSample(bestCalibSample);

        if (!calibrationInitialized) {
          calibrationSampleCount++;
          timeCalibrationMs = CALIBRATION_EWMA_ALPHA * bestCalibSample + (1 - CALIBRATION_EWMA_ALPHA) * timeCalibrationMs;
          if (calibrationSampleCount >= CALIBRATION_MIN_SAMPLES) {
            timeCalibrationMs = Math.max(CALIBRATION_MIN_REASONABLE, Math.min(CALIBRATION_MAX_REASONABLE, timeCalibrationMs));
            calibrationInitialized = true;
            diagLogger.log('DIAG-CALIB', JSON.stringify({
              event: 'initialized',
              calibMs: Math.round(timeCalibrationMs),
              samples: calibrationSampleCount,
            }));
          }
        } else {
          const now = Date.now();
          const timeSinceReset = now - calibrationLastResetTime;
          const rate = Math.abs(bestCalibSample - timeCalibrationMs);
          if (timeSinceReset > CALIBRATION_RESET_COOLDOWN && rate > CALIBRATION_MAX_RATE_MS) {
            calibrationSampleCount = 0;
            calibrationLastResetTime = now;
            calibrationInitialized = false;
            diagLogger.log('DIAG-CALIB', JSON.stringify({
              event: 'reset',
              reason: 'large_drift',
              rate: Math.round(rate),
              timeSinceReset: timeSinceReset,
            }));
          } else {
            const rawNew = CALIBRATION_EWMA_ALPHA * bestCalibSample + (1 - CALIBRATION_EWMA_ALPHA) * timeCalibrationMs;
            timeCalibrationMs = Math.max(CALIBRATION_MIN_REASONABLE, Math.min(CALIBRATION_MAX_REASONABLE, rawNew));
          }
        }
      }
    }
  }

  let closestRecord: { timestamp: number; boxes: any[]; timestamp_ms?: number } | null = null;
  let minDiff = Infinity;

  for (const record of detectionBuffer.value) {
    const recordTimestamp = record.timestamp_ms || record.timestamp;
    // 【P0修复】：只匹配过去或当前的检测框，防止检测框残留
    const timeDiff = calibratedVideoTime - recordTimestamp;
    if (timeDiff >= 0 && timeDiff < CALIBRATION_SYNC_WINDOW_MS && timeDiff < minDiff) {
      minDiff = timeDiff;
      closestRecord = record;
    }
  }

  if (!closestRecord && detectionBuffer.value.length > 0 && playMode.value === 'live') {
    const lastRecord = detectionBuffer.value[detectionBuffer.value.length - 1];
    const timeSinceLastDetectionMs = Date.now() - lastDetectionMessageTime;
    if (timeSinceLastDetectionMs < 5000 && lastRecord) {
      closestRecord = lastRecord;
      diagLogger.log('DIAG-LASTRESORT', JSON.stringify({
        event: 'using_newest_detection',
        bufferSize: detectionBuffer.value.length,
        lastTs: lastRecord.timestamp,
        calibMs: Math.round(timeCalibrationMs),
        videoAbs: Math.round(currentVideoAbsTime),
        calibrated: calibrationInitialized,
      }));
    }
  }

  if (playMode.value !== 'history') {
    const timeSinceLastDetection = Date.now() - lastDetectionMessageTime
    if (timeSinceLastDetection > 30000) {
      if (!isDetectionStale.value && Date.now() - lastStaleLog > 5000) {
        isDetectionStale.value = true;
        lastStaleLog = Date.now();
        diagLogger.log('DIAG-STALE', JSON.stringify({
          event: 'stale_triggered',
          timeSinceLastDetectionMs: timeSinceLastDetection,
          bufferSize: detectionBuffer.value.length,
          lastDetectionMsgTime: new Date(lastDetectionMessageTime).toISOString(),
          currentTime: new Date().toISOString(),
          wsReadyState: ws?.readyState,
        }))
        scheduleStaleReconnect();
      }
    } else {
      if (isDetectionStale.value) {
        logger.info('system', `Detection recovered after ${timeSinceLastDetection}ms stale`);
        diagLogger.log('DIAG-STALE', JSON.stringify({ event: 'stale_recovered', timeSinceLastDetectionMs: timeSinceLastDetection }))
      }
      isDetectionStale.value = false;
      cancelStaleReconnect();
    }
  } else {
    isDetectionStale.value = false;
    cancelStaleReconnect();
  }

  // 【诊断日志】：如果缓冲池有数据但没对上时间，记录漂移量（每 3 秒记录一次）
  if (!closestRecord && detectionBuffer.value.length > 0) {
    if (!lastDriftLog || Date.now() - lastDriftLog > 3000) {
      const first = detectionBuffer.value[0];
      const last = detectionBuffer.value[detectionBuffer.value.length - 1];
      const drift = currentVideoAbsTime - first.timestamp;
      const driftLast = currentVideoAbsTime - last.timestamp;
      const calibratedDrift = (currentVideoAbsTime - timeCalibrationMs) - last.timestamp;
      diagLogger.log('DIAG-DRIFT', JSON.stringify({ 
        driftFirstMs: Math.round(drift), 
        driftLastMs: Math.round(driftLast),
        calibratedDriftLastMs: Math.round(calibratedDrift),
        bufferSize: detectionBuffer.value.length,
        videoAbs: currentVideoAbsTime,
        dataFirstAbs: first.timestamp,
        dataLastAbs: last.timestamp,
        calibrationMs: Math.round(timeCalibrationMs),
        cachedBufferDelayMs: Math.round(cachedBufferDelayMs),
        calibrated: calibrationInitialized
      }));
      lastDriftLog = Date.now();
    }
  }

  if (Date.now() - lastRenderDiagLog > 5000) {
    const bufferTsRange = detectionBuffer.value.length > 0
      ? `[${detectionBuffer.value[0].timestamp}..${detectionBuffer.value[detectionBuffer.value.length - 1].timestamp}]`
      : '[]';
    diagLogger.log('DIAG-RENDER', JSON.stringify({
      hasClosest: !!closestRecord,
      boxesCount: closestRecord?.boxes.length ?? 0,
      minDiff: closestRecord ? Math.round(minDiff) : null,
      bufferSize: detectionBuffer.value.length,
      bufferTsRange,
      playMode: playMode.value,
      isPaused: isVideoPaused.value,
      calibrated: calibrationInitialized,
      calibMs: Math.round(timeCalibrationMs),
      cachedBufferDelayMs: Math.round(cachedBufferDelayMs),
      videoTime: video.currentTime.toFixed(2),
      videoAbs: Math.round(currentVideoAbsTime),
      effectiveTime: Math.round(calibrationInitialized ? currentVideoAbsTime - timeCalibrationMs : currentVideoAbsTime - (cachedBufferDelayMs > 0 ? cachedBufferDelayMs : 3000)),
      pendingRecords: pendingRecords.value.length,
      recordsCount: records.value.length,
      stale: isDetectionStale.value,
      timeSinceLastDetection: Math.round(Date.now() - lastDetectionMessageTime),
      videoResolution: `${video.videoWidth}x${video.videoHeight}`,
      taskResolution: `${currentTask.value?.resolution_width}x${currentTask.value?.resolution_height}`,
      absTimeSource: _lastAbsTimeSource,
    }))
    lastRenderDiagLog = Date.now()
  }
  if (closestRecord && closestRecord.boxes.length > 0) {
    lastValidRecord.value = { timestamp: closestRecord.timestamp, boxes: closestRecord.boxes };
    lastValidRecordUpdateTime = Date.now()
  } else if (closestRecord && closestRecord.boxes.length === 0) {
    lastValidRecord.value = null;
  }

  rafId = requestAnimationFrame(renderLoop);
}

function onTimeUpdate(e: Event) {
  const video = e.target as HTMLVideoElement;
  
  // 历史模式：到达定格时长时暂停
  if (playMode.value === 'history' && frozenDuration.value > 0) {
    if (video.currentTime >= frozenDuration.value - 0.2) {
      video.pause();
      return; 
    }
  }

  if (playMode.value !== 'live') {
    // 历史模式
    if (isNaN(video.duration) || !isFinite(video.duration)) {
      const currentLevel = hls?.currentLevel ?? -1;
      const levelDetails = (currentLevel >= 0) ? hls?.levels?.[currentLevel]?.details : null;
      if (levelDetails) {
        totalDuration.value = levelDetails.totalduration;
      }
    } else {
      totalDuration.value = video.duration;
    }
  }

  if (!isUserSeeking.value && !isSwitchingStream) {
    currentGlobalTime.value = video.currentTime;
  }

  // 【滑动窗口机制】：如果在历史模式下，且视频播放快到了我们当前数据的边缘
  if (playMode.value === 'history' && !isSwitchingStream) {
    const currentPhysicalTimeMs = offsetToAbsTime(video.currentTime);
    
    // 如果当前播放时间距离我们抓取的数据边界不到 5 秒了，悄悄在后台拉取下一个 30 秒的切片
    if (historyLastFetchTime - currentPhysicalTimeMs < 5000) {
        loadHistoricalBoxes(historyLastFetchTime, historyLastFetchTime + 30000);
    }
  }
}

function toggleChannel() {
  if (!hls || !videoPlayerRef.value) return;

  const timeToRestore = videoPlayerRef.value.currentTime;
  const isPaused = videoPlayerRef.value.paused;

  isSwitchingStream = true;
  lastValidRecord.value = null;
  timeCalibrationMs = 0;
  calibrationInitialized = false;

  currentChannel.value = currentChannel.value === 'annotated' ? 'ir_annotated' : 'annotated';

  if (playbackMode.value === 'webrtc') {
    switchToHLS();
  }
  
  const newStreamUrl = `/storage/${props.taskId}/stream_${currentChannel.value}.m3u8`;

  hls.loadSource(newStreamUrl);

  hls.once(Hls.Events.MANIFEST_PARSED, () => {
    if (videoPlayerRef.value) {
      videoPlayerRef.value.currentTime = timeToRestore;
      if (!isPaused) {
        videoPlayerRef.value.play().catch(() => {});
      }
    }
    setTimeout(() => {
      if (isSwitchingStream) {
        isSwitchingStream = false;
      }
    }, 1500);
  });

  setTimeout(() => {
    if (isSwitchingStream) {
      isSwitchingStream = false;
      lastValidRecord.value = null;
    }
  }, 1500);
  
  message.success(`已切换至 ${currentChannel.value === 'ir_annotated' ? 'IR标注流' : 'RGB标注流'}`);
  sendDebugLog('CHANNEL_SWAP', { channel: currentChannel.value, time: timeToRestore });
}

function handleSeekBarHover(e: MouseEvent) {
  if (!seekbarWrapRef.value || totalDuration.value <= 0) return;

  const rect = seekbarWrapRef.value.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const percent = Math.max(0, Math.min(1, x / rect.width));
  
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
      const absStart = offsetToAbsTime(startSec);
    const absEnd = offsetToAbsTime(endSec);

    const res = await tasksApi.getDetectionRecords(props.taskId, {
      start_time: new Date(absStart).toISOString(),
      end_time: new Date(absEnd).toISOString(),
      limit: 5000,
    })

    // 将后端的历史数据转换为缓冲池格式
    const historicalData = (res.records || []).map((rec: DetectionRecord) => ({
      timestamp: new Date(rec.detected_at).getTime(),
      timestamp_ms: new Date(rec.detected_at).getTime(),
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
    logger.warn('api', 'History records failed to fetch', { taskId: props.taskId, error: String(e) })
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
      const newData = res
        .filter((d: any) => !existingTs.has(d.timestamp))
        .map((d: any) => ({
          ...d,
          timestamp_ms: d.timestamp_ms || d.timestamp,
        }));
      detectionBuffer.value = [...detectionBuffer.value, ...newData].sort((a, b) => a.timestamp - b.timestamp);
    }
    historyLastFetchTime = Math.max(historyLastFetchTime, endMs);
    sendDebugLog('LOAD_HISTORICAL_BOXES', { count: res?.length, range: [startMs, endMs] });
  } catch (e) {
    logger.error('api', 'Failed to load historical boxes', { taskId: props.taskId, error: String(e) });
  }
}

// 真正的流切换执行逻辑
let liveDetectionCache: Array<{ timestamp: number; boxes: any[] }> = []

const executeStreamSwitch = async (targetSeconds: number) => {
    isSwitchingStream = true;
    isDetectionStale.value = false;
    cancelStaleReconnect();
    
    const targetPhysicalTimeMs = Math.floor(offsetToAbsTime(targetSeconds));
    // V4.4: 三层 fallback 确保 historyStartMs 始终有效
    const historyStartMs = firstSessionStartTime.value
      || (currentTask.value?.session_start_time ? new Date(currentTask.value.session_start_time).getTime() : null)
      || (targetPhysicalTimeMs - frozenDuration.value * 1000)
      || (targetPhysicalTimeMs - 3600000);

    if (playMode.value === 'live') {
        logger.info('playback', 'Freezing live timeline, entering history mode', { taskId: props.taskId });
        skipPlayModeWatchInit = true;
        playMode.value = 'history';
        
        frozenDuration.value = totalDuration.value;
        frozenRealTime.value = offsetToAbsTime(targetSeconds);

        // 保留切换时刻的实时检测数据作为历史数据基底，标记为 history
        detectionBuffer.value.forEach(d => d.is_history = true);
        lastValidRecord.value = null;

        stopRunningSecondsCounter();
        
        if (playbackMode.value === 'webrtc') {
          switchToHLS();
        }
        
        if (videoPlayerRef.value) videoPlayerRef.value.pause();
        
        await loadHistoricalBoxes(historyStartMs, frozenRealTime.value);
        
        await loadHistory();
        
        initHls(targetSeconds);
    } else {
        if (videoPlayerRef.value) {
            videoPlayerRef.value.currentTime = targetSeconds;
            videoPlayerRef.value.play().catch(()=>{});
        }
        
        detectionBuffer.value = [];
        lastValidRecord.value = null;
        await loadHistoricalBoxes(targetPhysicalTimeMs - 30000, targetPhysicalTimeMs + 60000);
    }
};

// 暴露给进度条的防抖处理器 (延迟 400ms，拖拽滑块期间不发请求)
const debouncedSeek = debounce((targetSeconds: number) => {
    executeStreamSwitch(targetSeconds);
}, 400);

function handleGlobalSeek(targetPercent: number) {
  if (!videoPlayerRef.value || isNaN(targetPercent)) return;
  
  const targetSeconds = (targetPercent / 100) * (frozenDuration.value || totalDuration.value);
  
  isUserSeeking.value = true; 
  currentGlobalTime.value = targetSeconds;
  
  if (playMode.value === 'history') {
    if (videoPlayerRef.value) {
      videoPlayerRef.value.currentTime = targetSeconds;
    }
    const targetMs = Math.floor(offsetToAbsTime(targetSeconds));
    loadHistoricalBoxes(targetMs - 10000, targetMs + 60000);
  } else {
    debouncedSeek(targetSeconds);
  }
}

const debouncedLoadBoxes = debounce((targetMs: number) => {
  loadHistoricalBoxes(targetMs - 10000, targetMs + 60000);
}, 300);

function handleUnifiedPlayPause() {
  if (!videoPlayerRef.value) return;

  if (videoPlayerRef.value.paused) {
    // 恢复播放
    // 实时模式下取消暂停，跳转到最新直播进度
    if (playMode.value !== 'history' && hls) {
      if (hls.liveSyncPosition !== null) {
        videoPlayerRef.value.currentTime = Math.max(0, hls.liveSyncPosition);
      }
    } else if (playMode.value === 'history' && videoPlayerRef.value.duration > 0 && videoPlayerRef.value.currentTime >= videoPlayerRef.value.duration - 0.5) {
      videoPlayerRef.value.currentTime = 0;
    }
    
    // 【关键修复】恢复播放时如果 HLS 已销毁，重新初始化
    if (!hls && streamState.value !== 'exception') {
      logger.info('playback', 'HLS instance missing, reinitializing before play', { taskId: props.taskId });
      initHls();
    }
    
    videoPlayerRef.value.play().catch(e => {
      logger.error('playback', 'Playback failed', { taskId: props.taskId, error: String(e) });
      message.error("播放失败: 视频流可能已断开");
      // 播放失败时尝试重新连接
      if (streamState.value === 'running') {
        streamState.value = 'loading';
        setTimeout(() => initHls(), 1000);
      }
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

  isSwitchingStream = true;
  isDetectionStale.value = false;
  cancelStaleReconnect();

  playMode.value = 'live';
  isUserSeeking.value = false;
  frozenDuration.value = 0;
  frozenRealTime.value = 0;
  lastVideoAbsTime = 0;
  lastVideoAbsTimeTs = 0;
  hasFirstFrameRendered = false;

  detectionBuffer.value = [];
  liveDetectionCache = [];
  lastValidRecord.value = null;
  pendingRecords.value = [];
  lastEmittedAbsoluteTime = 0;
  
  sendDebugLog('JUMP_TO_LIVE');

  tasksApi.getSnapshot(props.taskId).then(snapshot => {
    if (snapshot?.task) {
      const baseSeconds = snapshot.task.cumulative_running_seconds || 0;
      const sessionStart = snapshot.task.session_start_time;
      if (sessionStart) {
        const sessionStartMs = new Date(sessionStart).getTime();
        const currentRunningSeconds = baseSeconds + (Date.now() - sessionStartMs) / 1000;
        startRunningSecondsCounter(currentRunningSeconds);
      } else {
        startRunningSecondsCounter(baseSeconds);
      }
    }
  }).catch(() => {
    startRunningSecondsCounter(totalDuration.value);
  });

  refreshRecords();

  if (webrtcStreamPath.value) {
    switchToWebRTC();
  } else {
    initHls();
  }
}

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
  
    stopEventsPolling()

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
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.video-player-container:has(.detection-records-panel) {
  height: 100%;
}

.video-player-container:not(:has(.detection-records-panel)) {
  height: 100%;
  min-height: 0;
  gap: 0;
}

.frame-container {
  flex: 1;
  min-width: 0;
  background: var(--bg-secondary, #1a1a2e);
  border-radius: 8px;
  overflow: hidden;
  position: relative;
  border: 1px solid var(--border-color, #333);
  box-shadow: inset 0 0 40px rgba(0, 0, 0, 0.15);
  display: flex;
  align-items: center;
  justify-content: center;
  background: #000;
}

video.video-frame {
  background-color: #000000 !important;
}

.video-frame {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
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
  background: rgba(0, 0, 0, 0.1);
  backdrop-filter: none;
  transition: all 0.3s ease;
}

.stale-detection-badge {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: rgba(255, 77, 79, 0.85);
  border-radius: 6px;
  backdrop-filter: blur(4px);
  animation: stale-pulse 2s ease-in-out infinite;
}

.stale-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #fff;
  animation: stale-dot-blink 1s ease-in-out infinite;
}

.stale-text {
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
}

@keyframes stale-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

@keyframes stale-dot-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
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

.mode-tag.is-webrtc {
  background: rgba(82, 196, 26, 0.15);
  color: #52c41a;
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
