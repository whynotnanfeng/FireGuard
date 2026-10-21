<template>
  <video
    ref="videoRef"
    autoplay
    muted
    playsinline
    class="webrtc-video"
    @playing="onPlaying"
    @error="onError"
  />
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'

const props = defineProps<{
  streamPath: string
  apiBase?: string
}>()

const emit = defineEmits<{
  (e: 'playing'): void
  (e: 'error', msg: string): void
  (e: 'stats', stats: { rttMs: number; bytesReceived: number }): void
}>()

const videoRef = ref<HTMLVideoElement | null>(null)

let pc: RTCPeerConnection | null = null
let statsInterval: number | null = null
let restartTimeout: number | null = null
let isActive = true

const ICE_SERVERS: RTCConfiguration = {
  iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
}

async function startWebRTC() {
  if (!props.streamPath || !isActive) return

  stopWebRTC()

  try {
    pc = new RTCPeerConnection(ICE_SERVERS)

    pc.addTransceiver('video', { direction: 'recvonly' })
    pc.addTransceiver('audio', { direction: 'recvonly' })

    pc.ontrack = (event) => {
      if (!videoRef.value || !isActive) return
      if (event.streams.length > 0) {
        videoRef.value.srcObject = event.streams[0]
      } else {
        const stream = new MediaStream()
        event.track && stream.addTrack(event.track)
        videoRef.value.srcObject = stream
      }
    }

    pc.oniceconnectionstatechange = () => {
      if (!pc) return
      const state = pc.iceConnectionState
      if (state === 'failed' || state === 'disconnected') {
        scheduleRestart()
      }
    }

    pc.onconnectionstatechange = () => {
      if (!pc) return
      if (pc.connectionState === 'failed') {
        scheduleRestart()
      }
    }

    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    const apiBase = props.apiBase || '/api'
    const resp = await fetch(`${apiBase}/webrtc?stream=${encodeURIComponent(props.streamPath)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp' },
      body: offer.sdp,
    })

    if (!resp.ok) {
      throw new Error(`WebRTC signaling failed: ${resp.status}`)
    }

    const contentType = resp.headers.get('content-type') || ''
    let answerSdp: string

    if (contentType.includes('application/sdp')) {
      answerSdp = await resp.text()
    } else {
      const json = await resp.json()
      if (json.sdp) {
        answerSdp = json.sdp
      } else {
        throw new Error('Invalid WebRTC answer format')
      }
    }

    await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSdp }))

    startStatsMonitor()
  } catch (e: any) {
    emit('error', e.message || 'WebRTC connection failed')
    scheduleRestart()
  }
}

function stopWebRTC() {
  if (statsInterval) {
    clearInterval(statsInterval)
    statsInterval = null
  }
  if (restartTimeout) {
    clearTimeout(restartTimeout)
    restartTimeout = null
  }
  if (pc) {
    pc.ontrack = null
    pc.oniceconnectionstatechange = null
    pc.onconnectionstatechange = null
    try {
      pc.close()
    } catch {}
    pc = null
  }
  if (videoRef.value) {
    videoRef.value.srcObject = null
  }
}

function scheduleRestart() {
  if (!isActive) return
  if (restartTimeout) return
  restartTimeout = window.setTimeout(() => {
    restartTimeout = null
    if (isActive) {
      startWebRTC()
    }
  }, 3000)
}

function startStatsMonitor() {
  if (statsInterval) clearInterval(statsInterval)
  statsInterval = window.setInterval(async () => {
    if (!pc) return
    try {
      const stats = await pc.getStats()
      stats.forEach((report) => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded') {
          emit('stats', {
            rttMs: report.currentRoundTripTime ? report.currentRoundTripTime * 1000 : 0,
            bytesReceived: report.bytesReceived || 0,
          })
        }
      })
    } catch {}
  }, 5000)
}

function onPlaying() {
  emit('playing')
}

function onError(e: Event) {
  const v = e.target as HTMLVideoElement
  const errMsg = v.error?.message || 'Unknown video error'
  emit('error', errMsg)
}

watch(() => props.streamPath, (newPath) => {
  if (newPath && isActive) {
    startWebRTC()
  }
})

onMounted(() => {
  isActive = true
  if (props.streamPath) {
    startWebRTC()
  }
})

onUnmounted(() => {
  isActive = false
  stopWebRTC()
})

defineExpose({ startWebRTC, stopWebRTC })
</script>

<style scoped>
.webrtc-video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background-color: #000;
}
</style>
