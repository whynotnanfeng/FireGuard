<template>
  <div class="dashboard-container">
    <div class="monitor-header">
      <div>
        <span class="monitor-title">Dashboard</span>
        <span class="monitor-count">{{ runningTasks.length }} running task(s)</span>
      </div>
      <button class="fullscreen-btn" id="fullscreenBtn" @click="toggleFullscreen">
        <svg viewBox="0 0 24 24" fill="currentColor">
            <path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/>
        </svg>
        Fullscreen
      </button>
    </div>

    <div class="video-grid" :class="{ fullscreen: isFullscreen }" id="videoGrid">
      <div class="video-cell" v-for="task in runningTasks" :key="task.id">
        <div class="video-cell-media" ref="mediaContainers" :data-task-id="task.id">
          <!-- Core scaling layer: uniform transform scale reproduces the large player's internal proportions -->
          <div class="video-scale-wrapper">
            <div class="video-scale-inner" :style="getScaleStyle(task.id)">
              <VideoPlayer 
                :task-id="task.id" 
                :ws-url="wsStreamUrl" 
                :show-records-panel="false" 
                class="dashboard-video-player" 
                @time-update="(time) => streamTimes[task.id] = time"
              />
            </div>
          </div>
          
          <span class="live-badge"><span class="dot"></span>LIVE</span>
          <div class="video-cell-overlay">
            <span class="video-cell-task-name">{{ task.name }}</span>
            <span class="video-cell-duration">{{ formatDuration(streamTimes[task.id] ?? task.cumulative_running_seconds) }}</span>
          </div>
        </div>
      </div>
      
      <!-- Empty states if no tasks -->
      <div class="video-cell empty-cell" v-if="runningTasks.length === 0">
        <div class="video-cell-media">
          <div class="video-cell-placeholder">
            <div class="icon">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
              </svg>
            </div>
            <div class="text">No running monitoring tasks</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { tasksApi, type Task } from '@/api/tasks'
import VideoPlayer from '@/components/VideoPlayer.vue'

const runningTasks = ref<Task[]>([])
const isFullscreen = ref(false)
let pollTimer: any = null
const mediaContainers = ref<HTMLElement[]>([])
const scaleMap = ref<Record<string, string>>({})
const streamTimes = ref<Record<string, number>>({})
let resizeObserver: ResizeObserver | null = null

const wsStreamUrl = computed(() => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  return `${protocol}//${host}/ws/stream`
})

// Baseline size: we use a 1024x640 (16:10) ratio so that 16:9 video is vertically centered with black bars at the bottom for the text overlay, avoiding occlusion of the picture.
const NATIVE_WIDTH = 1024
const NATIVE_HEIGHT = 640

async function fetchRunningTasks() {
  try {
    const res = await tasksApi.list({ limit: 100, status: 'running', task_type: 'stream' })
    runningTasks.value = res.items
    nextTick(() => {
      updateObservers()
    })
  } catch (e) {
    console.error('Failed to fetch running tasks', e)
  }
}

function updateObservers() {
  if (resizeObserver) {
    resizeObserver.disconnect()
  }
  
  resizeObserver = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const target = entry.target as HTMLElement
      const taskId = target.getAttribute('data-task-id')
      if (taskId) {
        const { width } = entry.contentRect
        const scale = width / NATIVE_WIDTH
        scaleMap.value[taskId] = `scale(${scale})`
      }
    }
  })
  
  if (mediaContainers.value) {
    mediaContainers.value.forEach(el => {
      if (el) resizeObserver!.observe(el)
    })
  }
}

function getScaleStyle(taskId: string) {
  const scale = scaleMap.value[taskId] || 'scale(1)'
  return {
    transform: scale,
    width: `${NATIVE_WIDTH}px`,
    height: `${NATIVE_HEIGHT}px`
  }
}

function formatDuration(seconds: number) {
  if (!seconds || isNaN(seconds)) return '00:00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

function toggleFullscreen() {
  isFullscreen.value = !isFullscreen.value
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && isFullscreen.value) {
    isFullscreen.value = false
  }
}

onMounted(() => {
  fetchRunningTasks()
  pollTimer = setInterval(fetchRunningTasks, 5000)
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (resizeObserver) resizeObserver.disconnect()
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
:root {
    --primary-blue: #4a90d9;
    --primary-hover: #3a7bc8;
    --primary-light: rgba(74, 144, 217, 0.08);
    --danger-red: #e85d26;
    --danger-light: rgba(232, 93, 38, 0.08);
    --bg-primary: #f5f7fa;
    --bg-card: #ffffff;
    --bg-media: #0a0a0a;
    --text-primary: #1e293b;
    --text-secondary: #64748b;
    --text-muted: #94a3b8;
    --border-color: #e2e8f0;
    --shadow-card: 0 1px 3px rgba(0, 0, 0, 0.06), 0 1px 2px rgba(0, 0, 0, 0.04);
    --radius-sm: 6px;
    --font-body: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    --font-mono: 'SF Mono', 'Fira Code', 'Consolas', monospace;
}

.dashboard-container {
    max-width: 100%;
}

@keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(232, 93, 38, 0.4); }
    50% { opacity: 0.7; box-shadow: 0 0 0 4px rgba(232, 93, 38, 0); }
}

.monitor-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0;
    background: var(--bg-card, #ffffff);
    border: 1px solid var(--border-color, #e2e8f0);
    border-bottom: none;
    padding: 12px 16px;
    border-radius: 0;
}

.monitor-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary, #1e293b);
}

.monitor-count {
    font-size: 12px;
    color: var(--text-muted, #94a3b8);
    margin-left: 8px;
    font-weight: 400;
}

.fullscreen-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border: 1px solid var(--border-color, #e2e8f0);
    background: var(--bg-card, #ffffff);
    border-radius: var(--radius-sm, 6px);
    font-size: 12px;
    color: var(--text-secondary, #64748b);
    cursor: pointer;
    transition: all 0.15s;
    font-family: var(--font-body);
}

.fullscreen-btn:hover {
    border-color: var(--primary-blue, #4a90d9);
    color: var(--primary-blue, #4a90d9);
    background: var(--primary-light, rgba(74, 144, 217, 0.08));
}

.fullscreen-btn svg {
    width: 14px;
    height: 14px;
}

.video-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0;
    border: 1px solid var(--border-color, #e2e8f0);
    border-top: none;
    border-radius: 0;
    overflow: hidden;
}

.video-cell {
    background: var(--bg-card, #ffffff);
    border: 1px solid var(--border-color, #e2e8f0);
    border-radius: 0;
    overflow: hidden;
    box-shadow: var(--shadow-card, 0 1px 3px rgba(0, 0, 0, 0.06));
    transition: all 0.15s;
}

.video-cell:hover {
    border-color: var(--primary-blue, #4a90d9);
    box-shadow: var(--shadow-card, 0 1px 3px rgba(0, 0, 0, 0.06)), 0 0 0 1px var(--primary-blue, #4a90d9);
}

.video-cell-media {
    position: relative;
    aspect-ratio: 16 / 10;
    background: var(--bg-media, #0a0a0a);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}

/* Core scaling container */
.video-scale-wrapper {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
}

.video-scale-inner {
    transform-origin: 0 0;
    /* transition: transform 0.1s ease-out; */
}

.dashboard-video-player {
    width: 100%;
    height: 100%;
    pointer-events: none; /* disable accidental native control bar clicks in the shrunken panel */
}

/* Strip the native player's extra margins and background so it blends in cleanly */
:deep(.dashboard-video-player .video-player-container) {
    height: 100% !important;
    min-height: 0 !important;
    gap: 0;
}
:deep(.dashboard-video-player .video-player) {
    border-radius: 0 !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}

/* Forcefully hide the native controls since the dashboard manages the UI with its own overlay */
:deep(.dashboard-video-player .controls),
:deep(.dashboard-video-player .history-seekbar-container),
:deep(.dashboard-video-player .detection-records-panel) {
    display: none !important;
}

.video-cell-placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: rgba(255, 255, 255, 0.25);
    gap: 6px;
    z-index: 2;
}

.video-cell-placeholder .icon {
    width: 32px;
    height: 32px;
    border: 1.5px dashed rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
}

.video-cell-placeholder .text {
    font-size: 11px;
}

.live-badge {
    position: absolute;
    top: 8px;
    left: 8px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 8px;
    background: rgba(232, 93, 38, 0.15);
    border: 1px solid rgba(232, 93, 38, 0.3);
    border-radius: 20px;
    color: var(--danger-red, #e85d26);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.5px;
    backdrop-filter: blur(4px);
    z-index: 10;
}

.live-badge .dot {
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--danger-red, #e85d26);
    animation: pulse 1.5s infinite;
}

.video-cell-overlay {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 6px 10px;
    background: linear-gradient(transparent, rgba(0, 0, 0, 0.75));
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    z-index: 10;
}

.video-cell-task-name {
    font-size: 11px;
    font-weight: 500;
    color: #fff;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.video-cell-duration {
    font-size: 10px;
    font-family: var(--font-mono, 'SF Mono', monospace);
    color: rgba(255, 255, 255, 0.7);
    flex-shrink: 0;
}

/* Fullscreen mode */
.video-grid.fullscreen {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 9999;
    background: #0a0a0a;
    padding: 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    overflow-y: auto;
    align-content: flex-start;
}

.video-grid.fullscreen .video-cell {
    width: 25%;
    border: 1px solid rgba(40, 40, 40, 0.8);
    border-radius: 0;
    box-shadow: none;
}

.video-grid.fullscreen .video-cell-media {
    aspect-ratio: 16 / 10;
    height: auto;
}

.video-grid.fullscreen .video-cell:hover {
    border-color: rgba(74, 144, 217, 0.5);
}
</style>
