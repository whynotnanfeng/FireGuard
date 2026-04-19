<template>
  <div class="video-player">
    <div v-if="errorMsg" class="error-msg">
      <WarningOutlined /> {{ errorMsg }}
    </div>

    <div v-else-if="!frameSrc" class="loading-state">
      <a-spin tip="正在连接视频流..." />
    </div>

    <div v-else class="frame-container">
      <img :src="frameSrc" class="video-frame" />
      
      <!-- Overlays for detections -->
      <div 
        v-for="(det, idx) in detections" 
        :key="idx"
        class="detection-box"
        :style="{
          left: `${det.box[0]}px`,
          top: `${det.box[1]}px`,
          width: `${det.box[2] - det.box[0]}px`,
          height: `${det.box[3] - det.box[1]}px`,
          borderColor: det.class === 'fire' ? '#FF3030' : '#FFB347'
        }"
      >
        <span class="detection-label" :class="{ 'is-fire': det.class === 'fire' }">
          {{ det.class }} {{(det.confidence * 100).toFixed(0)}}%
        </span>
      </div>

      <div class="controls">
        <a-button 
          v-if="status === 'running' || status === 'paused'"
          type="primary" 
          shape="circle"
          @click="togglePause"
        >
          <template #icon>
            <PauseCircleOutlined v-if="status === 'running'" />
            <PlayCircleOutlined v-else />
          </template>
        </a-button>
        <a-button danger shape="circle" @click="stop">
          <template #icon><CloseOutlined /></template>
        </a-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { tasksApi } from '@/api/tasks'
import {
  WarningOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  CloseOutlined
} from '@ant-design/icons-vue'

const props = defineProps<{
  taskId: string
  wsUrl: string
}>()

const emit = defineEmits(['close', 'status-change'])

const frameSrc = ref('')
const detections = ref<any[]>([])
const status = ref('running')
const errorMsg = ref('')

let ws: WebSocket | null = null

onMounted(() => {
  connect()
})

onUnmounted(() => {
  disconnect()
})

function connect() {
  const token = localStorage.getItem('token')
  ws = new WebSocket(`${props.wsUrl}?token=${token}`)

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      if (data.type === 'frame') {
        frameSrc.value = `data:image/jpeg;base64,${data.data}`
        detections.value = data.detections || []
      } else if (data.type === 'error') {
        errorMsg.value = data.message
      } else if (data.type === 'status') {
        status.value = data.status
        emit('status-change', data.status)
      }
    } catch (e) {
      console.error('Failed to parse WS message', e)
    }
  }

  ws.onclose = () => {
    if (!errorMsg.value && status.value !== 'ended') {
       errorMsg.value = '视频流连接断开'
    }
  }
}

function disconnect() {
  if (ws) {
    ws.close()
    ws = null
  }
}

async function togglePause() {
  try {
    if (status.value === 'running') {
      await tasksApi.pause(props.taskId)
      status.value = 'paused'
    } else {
      await tasksApi.execute(props.taskId)
      status.value = 'running'
    }
    emit('status-change', status.value)
  } catch (e) {
    console.error(e)
  }
}

function stop() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'stop' }))
  }
  emit('close')
}
</script>

<style scoped>
.video-player {
  width: 100%;
  background: var(--bg-secondary);
  border-radius: 8px;
  overflow: hidden;
  position: relative;
  min-height: 400px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-color);
}

.error-msg {
  color: var(--danger-red);
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
}

.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 400px;
}

.frame-container {
  position: relative;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.video-frame {
  max-width: 100%;
  max-height: 80vh;
  object-fit: contain;
}

.controls {
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 16px;
  background: rgba(0,0,0,0.6);
  padding: 10px 20px;
  border-radius: 30px;
  backdrop-filter: blur(4px);
  opacity: 0;
  transition: opacity 0.3s;
}

.frame-container:hover .controls {
  opacity: 1;
}
</style>
