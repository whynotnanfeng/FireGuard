<template>
  <div class="task-viewer">
    <div v-if="!result && !error" class="loading-state">
      <a-spin tip="加载任务结果..." />
    </div>
    <div v-else-if="error" class="loading-state">
      <a-empty description="结果加载失败或已过期" />
    </div>

    <template v-else>
      <div class="window-titlebar">
        <div class="titlebar-left">
          <h2>{{ taskName }}</h2>
          <span class="task-type-badge" :class="effectiveType">{{ typeLabel }}</span>
          <span class="status-dot" :class="taskStatus"></span>
        </div>
        <div class="titlebar-right">
          <button class="titlebar-btn" title="关闭" @click="$emit('close')">×</button>
        </div>
      </div>

      <div class="window-body">
        <div class="media-panel">
          <template v-if="effectiveType === 'image'">
            <div class="media-viewport image-viewport">
              <img v-if="currentImageUrl" :src="currentImageUrl" class="result-image" />
              <div v-else class="media-placeholder">
                <div class="icon-placeholder">▢</div>
                <span>{{ currentImageName }}</span>
                <span class="placeholder-sub">16:9 标注结果预览区</span>
              </div>
              <div class="media-nav prev" v-if="imageCount > 1" @click="prevImage">‹</div>
              <div class="media-nav next" v-if="imageCount > 1" @click="nextImage">›</div>
              <div class="media-dots" v-if="imageCount > 1">
                <span v-for="(_, i) in imageCount" :key="i" class="media-dot" :class="{ active: i === currentImageIndex }" @click="goToImage(i)"></span>
              </div>
            </div>
            <div class="media-controls">
              <div class="media-controls-left">
                <span class="media-index">{{ currentImageIndex + 1 }} / {{ imageCount }}</span>
              </div>
              <div class="media-controls-center"></div>
              <div class="media-controls-right">
                <button class="ctrl-btn" title="放大" @click="toggleZoom">⊕</button>
                <button class="ctrl-btn" title="下载" @click="downloadFile">↓</button>
              </div>
            </div>
          </template>

          <template v-else-if="effectiveType === 'video'">
            <div class="media-viewport video-viewport">
              <video v-if="currentImageUrl" :key="currentImageUrl" ref="videoRef" :src="currentImageUrl" class="result-video" controls @timeupdate="onVideoTimeUpdate" @loadedmetadata="onVideoLoaded"></video>
              <div v-else class="media-placeholder">
                <div class="icon-placeholder">▢</div>
                <span>{{ currentImageName }}</span>
                <span class="placeholder-sub">视频结果预览区</span>
              </div>
              <div class="media-nav prev" v-if="imageCount > 1" @click="prevImage">‹</div>
              <div class="media-nav next" v-if="imageCount > 1" @click="nextImage">›</div>
              <div class="media-dots" v-if="imageCount > 1">
                <span v-for="(_, i) in imageCount" :key="i" class="media-dot" :class="{ active: i === currentImageIndex }" @click="goToImage(i)"></span>
              </div>
            </div>
            <div class="media-controls">
              <div class="media-controls-left">
                <span class="media-index">{{ currentImageIndex + 1 }} / {{ imageCount }}</span>
              </div>
              <div class="media-controls-center"></div>
              <div class="media-controls-right">
                <button class="ctrl-btn" title="下载" @click="downloadFile">↓</button>
              </div>
            </div>
          </template>

          <template v-else-if="effectiveType === 'stream'">
            <div class="media-viewport stream-viewport">
              <VideoPlayer v-if="taskId && wsUrl" :task-id="taskId" :ws-url="wsUrl" :show-records-panel="false" :key="'vp-' + taskId" @close="$emit('close')" @time-update="handleTimeUpdate" @playback-absolute-time="handlePlaybackAbsoluteTime" @play-mode-change="handlePlayModeChange" />
            </div>
          </template>
        </div>

        <div class="info-panel">
          <div class="info-header">
            <div class="info-header-top">
              <span class="info-file-name" :title="effectiveType === 'stream' ? '目标检测记录' : currentFileName">{{ effectiveType === 'stream' ? '目标检测记录' : currentFileName }}</span>
              <span class="info-file-index" v-if="effectiveType === 'image' || effectiveType === 'video'">{{ currentImageIndex + 1 }} / {{ imageCount }}</span>
              <span class="info-file-index" v-else-if="effectiveType === 'stream'">{{ (result?.status === 'running' || result?.status === 'initializing' || !result) ? '运行中' : '视频回放' }}</span>
            </div>
            <div class="info-meta-row">
              <template v-if="effectiveType === 'stream'">
                <span class="info-meta-item"><span class="label">已运行</span> <span class="value">{{ elapsedDisplay }}</span></span>
                <span class="info-meta-item"><span class="label">FPS</span> <span class="value">{{ fpsDisplay }}</span></span>
              </template>
              <template v-else>
                <span class="info-meta-item"><span class="label">模型</span> <span class="value">{{ modelName }}</span></span>
                <span class="info-meta-item" v-if="effectiveType === 'video'"><span class="label">输入</span> <span class="value">RGB</span></span>
              </template>
            </div>
          </div>

          <div class="info-content" :class="{ 'stream-mode': effectiveType === 'stream' }">
            <DetectionEventsTable
              v-if="effectiveType === 'stream'"
              :events="pagedRecords"
              :total="recTotal"
              :filter="recFilter"
              :page="recPage"
              :page-size="recPageSize"
              :active-count="summaryActive"
              :total-count="summaryTotal"
              :at-time="historyAtMs"
              @update:filter="recFilter = $event"
              @update:page="recPage = $event"
              @update:page-size="onRecordPageSizeChange"
            />

            <template v-else>
              <ResultStatsPanel
                :kind="effectiveType"
                :current-box-count="currentBoxCount"
                :total-records="totalRecords"
                :current-category-counts="currentCategoryCounts"
                :category-distribution="categoryDistribution"
                :max-category-count="maxCategoryCount"
              />
            </template>

          </div>

          <div class="info-footer" v-if="effectiveType !== 'stream'">
            <button class="footer-btn" @click="downloadFile">↓ 当前结果</button>
            <button class="footer-btn primary" @click="downloadAll">↓ 全部 ZIP</button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { tasksApi, type DetectionEvent } from '@/api/tasks'
import VideoPlayer from '@/components/VideoPlayer.vue'
import DetectionEventsTable from '@/components/DetectionEventsTable.vue'
import ResultStatsPanel from '@/components/ResultStatsPanel.vue'
import type { EventFilter } from '@/composables/useDetectionFeed'

const props = defineProps<{ taskId: string; taskType?: string; wsUrl?: string }>()
const emit = defineEmits<{ close: [] }>()

const effectiveType = computed(() => props.taskType || 'image')

const result = ref<any>(null)
const error = ref(false)
const currentImageIndex = ref(0)
const zipping = ref(false)
const videoRef = ref<HTMLVideoElement | null>(null)
const videoCurrentTime = ref(0)
const videoDuration = ref(0)
const detectionRecords = ref<DetectionEvent[]>([])
const recPage = ref(1)
const recPageSize = ref(10)
const recTotal = ref(0)
const recFilter = ref<EventFilter>('active')
const summaryActive = ref(0)
const summaryTotal = ref(0)
const streamRunningTime = ref(0)
const currentPlaybackAbsoluteTime = ref<number>(0)
// 播放器当前处于实时还是历史回放：只有历史回放才需要把右侧列表"倒回"到那一刻。
const playerPlayMode = ref<'live' | 'history'>('live')

function handlePlaybackAbsoluteTime(absTime: number) {
  currentPlaybackAbsoluteTime.value = absTime
}

function handlePlayModeChange(mode: 'live' | 'history') {
  if (playerPlayMode.value === mode) return
  playerPlayMode.value = mode
  if (mode === 'history') {
    // 切入历史瞬间播放器广播的绝对时间还停留在切换前的锚点，
    // 等它按 frozenRealTime 重算一次再拉列表，避免先闪一帧实时数据。
    recPage.value = 1
    window.setTimeout(loadDetectionRecords, 600)
    return
  }
  loadDetectionRecords()
}

// 历史回放的时间游标（按秒取整，避免逐帧刷新右侧表格）。
// 给出后，右侧"活跃/结束"与时长都按这一刻还原，而不是任务结束后的最终状态。
const historyAtMs = computed<number | undefined>(() => {
  if (playerPlayMode.value !== 'history') return undefined
  const t = currentPlaybackAbsoluteTime.value
  return t > 0 ? Math.round(t / 1000) * 1000 : undefined
})

// 统一将不带时区后缀的北京时间 (naive) 强制追加时区偏移量，与 HLS 携带的 UTC 时间戳达成基准物理对齐
const parseAbsoluteTime = (timeStr: string | null | undefined): number => {
  if (!timeStr) return 0
  let formatted = timeStr.trim().replace(' ', 'T')
  if (!formatted.match(/(Z|[+-]\d{2}:?\d{2})$/)) {
    formatted += '+08:00'
  }
  return new Date(formatted).getTime()
}

onMounted(async () => {
  await loadTask()
})

// 同一个组件实例切换任务时（例如父级不换 key），必须重新加载并清空旧任务记录。
watch(() => props.taskId, (newId) => {
  if (newId) loadTask()
})

async function loadTask() {
  error.value = false
  detectionRecords.value = []
  recPage.value = 1
  recTotal.value = 0
  currentImageIndex.value = 0
  result.value = null
  if (props.taskType === 'stream') {
    // 先挂载播放器，任务详情异步补全，避免打开弹窗时先白等一个请求。
    result.value = { type: 'stream', status: 'running' }
    loadDetectionRecords()
    startAutoRefresh()
    tasksApi.get(props.taskId).then((taskDetail) => {
      const actualFps = taskDetail.detection_config?.fps || taskDetail.detection_config?.frame_rate || '—'
      result.value = {
        type: 'stream',
        task_name: taskDetail.name,
        model_name: taskDetail.model_name,
        fps: actualFps,
        _session_start_time: taskDetail.session_start_time,
        _recording_seconds: taskDetail.cumulative_running_seconds || 0,
        started_at: taskDetail.created_at,
        status: taskDetail.status
      }
    }).catch(() => { /* 详情失败不影响播放，保留占位状态 */ })
    return
  }
  try {
    const res = await tasksApi.result(props.taskId)
    if (!res) throw new Error('Empty')
    result.value = res
    loadDetectionRecords()
  } catch (e) {
    error.value = true
  }
}

async function loadDetectionRecords() {
  try {
    const eventType = recFilter.value === 'active'
      ? 'enter'
      : recFilter.value === 'closed'
        ? 'leave'
        : undefined
    const atTime = historyAtMs.value
    const [res, summary] = await Promise.all([
      tasksApi.getDetectionEvents(props.taskId, {
        limit: recPageSize.value,
        skip: (recPage.value - 1) * recPageSize.value,
        order: 'desc',
        event_type: eventType,
        at_time: atTime,
      }),
      tasksApi.getDetectionEventsSummary(props.taskId, { at_time: atTime }),
    ])
    detectionRecords.value = res.events
    recTotal.value = res.total
    summaryActive.value = summary.active_targets
    summaryTotal.value = summary.total_targets
  } catch { /* no-op */ }
}

watch(recFilter, () => {
  recPage.value = 1
  loadDetectionRecords()
})
watch([recPage, recPageSize], () => {
  loadDetectionRecords()
})

function onRecordPageSizeChange(size: number) {
  recPageSize.value = size
  recPage.value = 1
}

let refreshTimer: number | null = null

function startAutoRefresh() {
  stopAutoRefresh()
  refreshTimer = window.setInterval(() => {
    loadDetectionRecords()
  }, 3000)
}

function stopAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

onUnmounted(() => {
  stopAutoRefresh()
})

const taskName = computed(() => result.value?.task_name || result.value?.name || props.taskId?.slice(0, 8) || '任务结果')
const taskStatus = computed(() => {
  if (effectiveType.value === 'stream') return result.value?.status || 'running'
  return result.value?.status || 'completed'
})
const typeLabel = computed(() => {
  const m: Record<string, string> = { image: 'IMAGE', video: 'VIDEO', stream: 'STREAM' }
  return m[effectiveType.value] || effectiveType.value?.toUpperCase() || ''
})
const modelName = computed(() => result.value?.model_name || result.value?.model || 'YOLOv8-Fire')
const fpsDisplay = computed(() => {
  if (effectiveType.value === 'stream') return result.value?.fps || '—'
  return result.value?.fps || '—'
})

const imageCount = computed(() => {
  const urls = result.value?.urls
  const filenames = result.value?.filenames
  return Array.isArray(urls) ? urls.length : Array.isArray(filenames) ? filenames.length : 0
})
const currentImageName = computed(() => result.value?.filenames?.[currentImageIndex.value] || '')
const currentImageUrl = computed(() => result.value?.urls?.[currentImageIndex.value] || '')
const currentFileName = computed(() => {
  if (effectiveType.value === 'image' || effectiveType.value === 'video') return currentImageName.value || '标注结果'
  return result.value?.filename || result.value?.name || '结果'
})

const currentImageDetections = computed(() => {
  if (!result.value?.detections) return []
  const target = currentImageName.value
  const base = target.replace(/^annotated_/, '')
  return result.value.detections[target] || result.value.detections[base] || result.value.detections['annotated_' + base] || []
})
const currentBoxCount = computed(() => {
  if (effectiveType.value === 'image' || effectiveType.value === 'video') {
    const d = currentImageDetections.value
    return Array.isArray(d) ? d.length : 0
  }
  return summaryActive.value
})
const totalRecords = computed(() => {
  if (effectiveType.value === 'image' || effectiveType.value === 'video') {
    const dets = result.value?.detections
    if (!dets) return 0
    return Object.values(dets).reduce((sum: number, arr: any) => sum + (Array.isArray(arr) ? arr.length : 0), 0)
  }
  return summaryTotal.value
})
const completedCount = computed(() => Math.max(0, summaryTotal.value - summaryActive.value))
const activeCount = computed(() => summaryActive.value)
const elapsedDisplay = computed(() => {
  if (effectiveType.value === 'stream') {
    if (streamRunningTime.value > 0) {
      return formatDuration(streamRunningTime.value)
    }
    // 已停止的任务：用累计录制时长，而不是"现在 - 会话起点"（会一直往上长）。
    const status = result.value?.status
    const recording = Number(result.value?._recording_seconds || 0)
    if (recording > 0 && status !== 'running' && status !== 'initializing') {
      return formatDuration(recording)
    }
    if (!result.value?._session_start_time && !result.value?.started_at) return '—'
    const start = new Date(result.value._session_start_time || result.value.started_at).getTime()
    const elapsed = Math.floor((Date.now() - start) / 1000)
    return formatDuration(elapsed)
  }
  return '—'
})

const currentCategoryCounts = computed(() => {
  const counts: Record<string, number> = {}
  if (effectiveType.value === 'image' || effectiveType.value === 'video') {
    const dets = currentImageDetections.value
    if (Array.isArray(dets)) {
      dets.forEach((d: any) => {
        if (!d) return
        const cls = d.class_name || 'Unknown'
        counts[cls] = (counts[cls] || 0) + 1
      })
    }
    return counts
  }

  const sourceEvents = (effectiveType.value === 'stream' && currentPlaybackAbsoluteTime.value > 0)
    ? detectionRecords.value.filter((rec: any) => {
        const enterTimeStr = rec.entered_at || rec.enter_time
        if (!enterTimeStr) return true
        const recordTime = parseAbsoluteTime(enterTimeStr)
        return recordTime <= currentPlaybackAbsoluteTime.value
      })
    : detectionRecords.value

  const dets = sourceEvents.filter(r => !r.left_at)
  if (Array.isArray(dets)) {
    dets.forEach((d: any) => {
      if (!d) return
      const cls = d.class_name || 'Unknown'
      counts[cls] = (counts[cls] || 0) + 1
    })
  }
  return counts
})

const categoryDistribution = computed(() => {
  const dist: Record<string, number> = {}
  if (effectiveType.value === 'image' || effectiveType.value === 'video') {
    const dets = result.value?.detections || {}
    Object.values(dets).forEach((arr: any) => {
      if (!Array.isArray(arr)) return
      arr.forEach((d: any) => {
        if (!d) return
        const cls = d.class_name || d.class || 'Unknown'
        dist[cls] = (dist[cls] || 0) + 1
      })
    })
  } else {
    const sourceEvents = (effectiveType.value === 'stream' && currentPlaybackAbsoluteTime.value > 0)
      ? detectionRecords.value.filter((rec: any) => {
          const enterTimeStr = rec.entered_at || rec.enter_time
          if (!enterTimeStr) return true
          const recordTime = parseAbsoluteTime(enterTimeStr)
          return recordTime <= currentPlaybackAbsoluteTime.value
        })
      : detectionRecords.value

    sourceEvents.forEach(r => {
      const cls = r.class_name || 'Unknown'
      dist[cls] = (dist[cls] || 0) + 1
    })
  }
  return dist
})
const maxCategoryCount = computed(() => Math.max(1, ...Object.values(categoryDistribution.value)))

const pagedRecords = computed(() => detectionRecords.value)

const detectionSegments = computed(() => {
  const recs = detectionRecords.value
  if (!recs.length || !videoDuration.value) return []
  const segments: { left: number; width: number }[] = []
  recs.forEach(r => {
    const t = r.entered_at
    if (!t) return
    const s = new Date(t).getTime()
    const startOffset = (s % 100000) / 1000
    const dur = (r.duration_ms || 0) / 1000
    if (dur > 0 && videoDuration.value > 0) {
      segments.push({ left: (startOffset / videoDuration.value) * 100, width: Math.min((dur / videoDuration.value) * 100, 5) })
    }
  })
  return segments
})

const seekPercent = computed(() => videoDuration.value > 0 ? (videoCurrentTime.value / videoDuration.value) * 100 : 0)

function prevImage() { if (currentImageIndex.value > 0) currentImageIndex.value-- }
function nextImage() { if (currentImageIndex.value < imageCount.value - 1) currentImageIndex.value++ }
function goToImage(i: number) { currentImageIndex.value = i }
function handleTimeUpdate(time: number) { streamRunningTime.value = time }

function toggleZoom() { /* open fullscreen image */ }
function toggleVideoPlay() { videoRef.value?.paused ? videoRef.value?.play() : videoRef.value?.pause() }
function onVideoTimeUpdate() { if (videoRef.value) videoCurrentTime.value = videoRef.value.currentTime }
function onVideoLoaded() { if (videoRef.value) videoDuration.value = videoRef.value.duration }
function seekVideo(e: MouseEvent) {
  if (!videoRef.value || !videoDuration.value) return
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  videoRef.value.currentTime = ((e.clientX - rect.left) / rect.width) * videoDuration.value
}
function refreshRecords() { recPage.value = 1; loadDetectionRecords() }

async function downloadFile() {
  const url = effectiveType.value === 'image' ? currentImageUrl.value : result.value?.url
  if (!url) return
  window.open(url, '_blank')
}
async function downloadAll() {
  zipping.value = true
  try {
    const blob = await tasksApi.downloadAllResults(props.taskId)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${taskName.value}_results.zip`
    a.click()
    URL.revokeObjectURL(url)
  } catch { /* no-op */ }
  zipping.value = false
}

function pauseStream() { /* pause stream */ }
function toggleFullscreen() { /* fullscreen */ }

function formatTime(seconds: number): string {
  if (!seconds || isNaN(seconds)) return '00:00'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

function formatDuration(seconds: number, isActive = false): string {
  const suffix = isActive ? '+' : ''
  const secs = Math.floor(seconds)
  if (secs < 60) return `${secs}s${suffix}`
  if (secs < 3600) {
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return s > 0 ? `${m}m ${s}s${suffix}` : `${m}m${suffix}`
  }
  if (secs < 86400) {
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    return m > 0 ? `${h}h ${m}m${suffix}` : `${h}h${suffix}`
  }
  const d = Math.floor(secs / 86400)
  const h = Math.floor((secs % 86400) / 3600)
  return h > 0 ? `${d}d ${h}h${suffix}` : `${d}d${suffix}`
}
</script>

<style scoped>
.task-viewer {
  --primary: #1677ff;
  --primary-light: #e6f4ff;
  --primary-hover: #4096ff;
  --success: #52c41a;
  --success-light: #f6ffed;
  --warning: #faad14;
  --warning-light: #fffbe6;
  --danger: #ff4d4f;
  --danger-light: #fff2f0;
  --bg-page: #f5f7fa;
  --bg-card: #ffffff;
  --bg-secondary: #fafafa;
  --bg-hover: #f5f5f5;
  --border: #e8e8e8;
  --border-light: #f0f0f0;
  --text-primary: #1a1a2e;
  --text-secondary: #595959;
  --text-muted: #8c8c8c;
  --text-placeholder: #bfbfbf;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.06);
  --shadow-lg: 0 8px 24px rgba(0,0,0,0.08);
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --font-body: 'Noto Sans SC', 'Plus Jakarta Sans', sans-serif;
  --font-mono: 'SF Mono', 'Fira Code', 'Consolas', monospace;

  background: var(--bg-card);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  border: 1px solid var(--border);
  font-family: var(--font-body);
  width: 100%;
  max-width: 1400px;
  margin: 0 auto;
}

.loading-state {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
}

.window-titlebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-light);
  background: var(--bg-secondary);
}

.titlebar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.titlebar-left h2 {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.task-type-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.task-type-badge.image { background: var(--success-light); color: var(--success); }
.task-type-badge.video { background: var(--primary-light); color: var(--primary); }
.task-type-badge.stream { background: var(--warning-light); color: #d48806; }

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.status-dot.running { background: var(--success); box-shadow: 0 0 6px rgba(82, 196, 26, 0.5); animation: pulse 2s infinite; }
.status-dot.completed { background: var(--primary); }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.titlebar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.titlebar-btn {
  width: 32px;
  height: 32px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 14px;
  transition: all 0.15s;
}

.titlebar-btn:hover {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-light);
}

.window-body {
  display: flex;
  height: 70vh;
  min-height: 500px;
}

.media-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #000;
  position: relative;
  min-width: 0;
}

.media-viewport {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.stream-viewport {
  flex: 1;
  display: flex;
  align-items: stretch;
  justify-content: stretch;
  position: relative;
  overflow: hidden;
}

.stream-viewport :deep(.video-player-container) {
  width: 100%;
  height: 100%;
  min-height: 0;
  gap: 0;
}

.stream-viewport :deep(.frame-container) {
  border-radius: 0;
  border: none;
  box-shadow: none;
}

.result-image {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.result-video {
  max-width: 100%;
  max-height: 100%;
  width: 100%;
}

.media-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: rgba(255,255,255,0.4);
  font-size: 13px;
  gap: 12px;
}

.media-placeholder .icon-placeholder {
  width: 64px;
  height: 64px;
  border: 2px dashed rgba(255,255,255,0.2);
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
}

.placeholder-sub {
  font-size: 11px;
  opacity: 0.6;
}

.media-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: rgba(0,0,0,0.5);
  backdrop-filter: blur(4px);
  border: 1px solid rgba(255,255,255,0.1);
  color: rgba(255,255,255,0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s;
  font-size: 16px;
  z-index: 10;
}

.media-nav:hover {
  background: rgba(0,0,0,0.7);
  color: #fff;
}

.media-nav.prev { left: 12px; }
.media-nav.next { right: 12px; }

.media-dots {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 6px;
  z-index: 10;
}

.media-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(255,255,255,0.3);
  cursor: pointer;
  transition: all 0.2s;
}

.media-dot.active {
  background: #fff;
  width: 18px;
  border-radius: 3px;
}

.media-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  background: rgba(0,0,0,0.8);
  backdrop-filter: blur(8px);
  border-top: 1px solid rgba(255,255,255,0.08);
}

.media-controls-left, .media-controls-center, .media-controls-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.media-controls-center { flex: 1; justify-content: center; }

.media-index {
  color: rgba(255,255,255,0.5);
  font-size: 12px;
  font-family: var(--font-mono);
}

.ctrl-btn {
  width: 32px;
  height: 32px;
  border: none;
  background: rgba(255,255,255,0.1);
  border-radius: 6px;
  color: rgba(255,255,255,0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.15s;
  font-size: 13px;
}

.ctrl-btn:hover { background: rgba(255,255,255,0.2); color: #fff; }
.ctrl-btn.primary { background: var(--primary); color: #fff; }
.ctrl-btn.primary:hover { background: var(--primary-hover); }

.live-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: rgba(255, 77, 79, 0.15);
  border: 1px solid rgba(255, 77, 79, 0.3);
  border-radius: 20px;
  color: #ff4d4f;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.live-badge .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #ff4d4f;
  animation: pulse 1.5s infinite;
}

.hls-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  background: rgba(22, 119, 255, 0.15);
  border: 1px solid rgba(22, 119, 255, 0.3);
  border-radius: 20px;
  color: #1677ff;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.seekbar-container {
  padding: 8px 16px;
  background: rgba(0,0,0,0.8);
}

.seekbar-label {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: rgba(255,255,255,0.5);
  margin-bottom: 6px;
  font-family: var(--font-mono);
}

.seekbar-track {
  width: 100%;
  height: 4px;
  background: rgba(255,255,255,0.15);
  border-radius: 2px;
  position: relative;
  cursor: pointer;
}

.seekbar-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 2px;
  width: 65%;
  position: relative;
}

.seekbar-fill::after {
  content: '';
  position: absolute;
  right: -5px;
  top: 50%;
  transform: translateY(-50%);
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0,0,0,0.3);
}

.seekbar-segments {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  display: flex;
}

.seekbar-segment {
  height: 100%;
  background: rgba(82, 196, 26, 0.4);
}

.info-panel {
  width: 420px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-card);
  border-left: 1px solid var(--border-light);
}

.info-header {
  padding: 16px 18px;
  border-bottom: 1px solid var(--border-light);
  background: var(--bg-secondary);
}

.info-header-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.info-file-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
}

.info-file-index {
  font-size: 11px;
  color: var(--text-muted);
  background: var(--bg-card);
  padding: 2px 8px;
  border-radius: 10px;
  border: 1px solid var(--border);
  font-family: var(--font-mono);
}

.info-meta-row {
  display: flex;
  gap: 16px;
  font-size: 11px;
  color: var(--text-muted);
}

.info-meta-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.info-meta-item .label { color: var(--text-placeholder); }
.info-meta-item .value { color: var(--text-secondary); font-weight: 500; }

.info-content {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

/* 流任务：列表区独立滚动，筛选/分页固定在面板上下两端。 */
.info-content.stream-mode {
  overflow: hidden;
}

.info-content::-webkit-scrollbar { width: 4px; }
.info-content::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

.info-footer {
  padding: 12px 18px;
  border-top: 1px solid var(--border-light);
  display: flex;
  gap: 8px;
  background: var(--bg-secondary);
}

.footer-btn {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-family: var(--font-body);
}

.footer-btn:hover {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-light);
}

.footer-btn.primary {
  background: var(--primary);
  color: #fff;
  border-color: var(--primary);
}

.footer-btn.primary:hover { background: var(--primary-hover); }
</style>
