/**
 * 实时 HLS 追帧：冷启动/漂移时把播放位置拉回直播边缘。
 *
 * 小偏差用轻微加速追帧，大偏差直接跳边缘；暂停/历史/拖拽时全部让位，
 * 避免与用户操作抢播放位置。
 */
import { type Ref } from 'vue'
import type Hls from 'hls.js'
import { diagLogger } from '@/utils/diagLogger'

export interface LiveDriftOptions {
  videoRef: Ref<HTMLVideoElement | null>
  getHls: () => Hls | null
  playMode: Ref<'live' | 'history'>
  isVideoPaused: Ref<boolean>
  isUserSeeking: Ref<boolean>
}

export function useLiveDrift(options: LiveDriftOptions) {
  let timer: number | null = null

  function seekToLiveAndPlay() {
    const video = options.videoRef.value
    const hls = options.getHls()
    if (!video || !hls) return
    // 用户主动暂停时不要被直播追帧逻辑重新播放。
    if (options.isVideoPaused.value) return
    const pos = hls.liveSyncPosition
    if (pos !== null && pos !== undefined && Number.isFinite(pos)) {
      // 冷启动时 HLS 可能只有 1–2 个分片，liveSyncPosition 为 null，浏览器会从 0 开始播；
      // 后续必须主动追到直播边缘，否则会越播越落后（曾累计到 90 秒以上）。
      const diff = pos - video.currentTime
      if (Math.abs(diff) > 0.75) {
        video.currentTime = Math.max(0, pos)
      }
    }
    video.play().catch(() => {})
    startLiveDriftCorrection()
  }

  function correctLiveDrift() {
    const video = options.videoRef.value
    const hls = options.getHls()
    if (!video || !hls || options.playMode.value !== 'live' || options.isVideoPaused.value || options.isUserSeeking.value) {
      return
    }
    const pos = hls.liveSyncPosition
    if (pos === null || pos === undefined || !Number.isFinite(pos)) return
    const diff = pos - video.currentTime
    if (diff > 12) {
      diagLogger.log('DIAG-LIVE-DRIFT', JSON.stringify({ action: 'seek', diff: Number(diff.toFixed(2)), currentTime: video.currentTime }))
      video.currentTime = Math.max(0, pos)
      video.playbackRate = 1
      video.play().catch(() => {})
    } else if (diff > 3) {
      video.playbackRate = Math.min(1.2, 1 + diff / 60)
    } else {
      video.playbackRate = 1
    }
  }

  function startLiveDriftCorrection() {
    if (options.playMode.value !== 'live') return
    if (timer !== null) return
    timer = window.setInterval(correctLiveDrift, 3000)
  }

  function stopLiveDriftCorrection() {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
    if (options.videoRef.value) {
      options.videoRef.value.playbackRate = 1
    }
  }

  return { seekToLiveAndPlay, correctLiveDrift, startLiveDriftCorrection, stopLiveDriftCorrection }
}
