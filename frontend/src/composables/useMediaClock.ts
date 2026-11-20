import type Hls from 'hls.js'
import { diagLogger } from '@/utils/diagLogger'

export interface SessionSlice {
  index: number
  start_offset_sec: number
  start_abs_time_ms: number
}

/**
 * 媒体时钟上下文。
 *
 * 全部用 getter 传入，避免 composable 持有播放器响应式对象：
 * 调用发生在帧回调里，读到的必须是最新值，而不是创建时的快照。
 */
export interface MediaClockContext {
  getVideo: () => HTMLVideoElement | null
  getHls: () => Hls | null
  getPlaybackMode: () => 'hls' | 'webrtc'
  getPlayMode: () => 'live' | 'history'
  getClockOffset: () => number
  getWebRtcLatencyMs: () => number
  isSwitching: () => boolean
  getFrozenRealTime: () => number
  getSessionStartTime: () => number
  getFirstSessionStartTime: () => number | null
  getSessionMap: () => SessionSlice[]
}

const HLS_FIXED_LATENCY_MS = 3000
const FIXED_OFFSET_MS = 100
const EXTRAPOLATION_MAX_MS = 5000

/**
 * 播放位置 → 绝对时间（epoch ms）。
 *
 * 优先级：HLS programDateTime → playingDate → 会话起点相对时间 → 外推 → 延迟估算。
 * 状态放在 composable 内部，重置由 `reset()` 统一完成，避免组件里散落多个时间变量。
 */
export function useMediaClock(ctx: MediaClockContext) {
  let lastVideoAbsTime = 0
  let lastVideoAbsTimeTs = 0
  let cachedBufferDelayMs = 6000
  let lastSource = ''

  function offsetToAbsTime(offsetSec: number): number {
    const sessionMap = ctx.getSessionMap()
    if (sessionMap.length === 0) {
      return (ctx.getFirstSessionStartTime() || 0) + offsetSec * 1000
    }
    let matched = sessionMap[0]
    for (let i = sessionMap.length - 1; i >= 0; i--) {
      if (offsetSec >= sessionMap[i].start_offset_sec) {
        matched = sessionMap[i]
        break
      }
    }
    const offsetWithinSession = offsetSec - matched.start_offset_sec
    return matched.start_abs_time_ms + offsetWithinSession * 1000
  }

  function rememberAbsTime(t: number, source: string) {
    lastVideoAbsTime = t
    lastVideoAbsTimeTs = Date.now()
    cachedBufferDelayMs = Date.now() + ctx.getClockOffset() - t
    lastSource = source
  }

  function getVideoAbsTime(): number {
    const video = ctx.getVideo()
    const hls = ctx.getHls()

    if (ctx.isSwitching() && ctx.getFrozenRealTime()) {
      return ctx.getFrozenRealTime()
    }

    if (ctx.getPlaybackMode() === 'webrtc' && ctx.getPlayMode() === 'live') {
      return Date.now() + ctx.getClockOffset() - FIXED_OFFSET_MS - ctx.getWebRtcLatencyMs()
    }

    // programDateTime 优先：每段 TS 分片携带绝对时间戳，精度高于会话起点推算。
    const currentTime = video ? video.currentTime : 0
    const currentLvl = hls?.currentLevel
    const levelIdx = currentLvl !== undefined && currentLvl >= 0 ? currentLvl : 0
    const details = hls?.levels?.[levelIdx]?.details

    if (details) {
      const fragments = details.fragments || []
      const currentFrag = fragments.find(
        (f: any) => currentTime >= f.start - 0.05 && currentTime < f.start + f.duration + 0.05
      )
      if (currentFrag?.programDateTime) {
        const pdtMs = typeof currentFrag.programDateTime === 'number'
          ? currentFrag.programDateTime
          : new Date(currentFrag.programDateTime).getTime()
        const t = pdtMs + (currentTime - currentFrag.start) * 1000
        rememberAbsTime(t, 'pdt')
        return t
      }

      // 防御性外推 1：currentTime 尚未达到首个分片（如初始加载时）
      if (fragments.length > 0) {
        const firstFrag = fragments[0]
        if (currentTime < firstFrag.start && firstFrag.programDateTime) {
          const pdtMs = typeof firstFrag.programDateTime === 'number'
            ? firstFrag.programDateTime
            : new Date(firstFrag.programDateTime).getTime()
          const t = pdtMs - (firstFrag.start - currentTime) * 1000
          rememberAbsTime(t, 'pdt_approx_past')
          return t
        }
      }

      // 防御性外推 2：currentTime 超过末尾分片（如临近直播边缘）
      if (fragments.length > 0) {
        const lastFrag = fragments[fragments.length - 1]
        if (currentTime >= lastFrag.start + lastFrag.duration && lastFrag.programDateTime) {
          const pdtMs = typeof lastFrag.programDateTime === 'number'
            ? lastFrag.programDateTime
            : new Date(lastFrag.programDateTime).getTime()
          const t = pdtMs + (currentTime - lastFrag.start) * 1000
          rememberAbsTime(t, 'pdt_approx_future')
          return t
        }
      }
    }

    if (hls?.playingDate) {
      const t = hls.playingDate.getTime()
      rememberAbsTime(t, 'playingDate')
      return t
    }

    // 回退: sessionStartTime 相对计算（PDT 不可用时）
    if (ctx.getFirstSessionStartTime() && ctx.getSessionStartTime() > 0) {
      const absTime = ctx.getSessionStartTime() + currentTime * 1000
      lastVideoAbsTime = absTime
      lastVideoAbsTimeTs = Date.now()
      lastSource = 'relative'
      return absTime
    }
    if (ctx.getFirstSessionStartTime() || ctx.getSessionMap().length > 0) {
      lastSource = 'session'
      return offsetToAbsTime(currentTime) - HLS_FIXED_LATENCY_MS
    }
    if (lastVideoAbsTime > 0 && (Date.now() - lastVideoAbsTimeTs) < EXTRAPOLATION_MAX_MS) {
      const t = lastVideoAbsTime + (Date.now() - lastVideoAbsTimeTs)
      if (lastSource !== 'extrapolate') {
        diagLogger.log('DIAG-ABSTIME', JSON.stringify({ source: 'extrapolate_fallback', t, prevSource: lastSource }))
      }
      lastSource = 'extrapolate'
      return t
    }

    // 终极回退：用 hls.latency 估算当前画面的真实延迟。
    let currentDelay = cachedBufferDelayMs
    if (ctx.getPlaybackMode() === 'hls' && hls && hls.latency >= 0 && hls.latency < 60) {
      currentDelay = hls.latency * 1000
      cachedBufferDelayMs = currentDelay
    }
    const t = Date.now() + ctx.getClockOffset() - currentDelay
    if (lastSource !== 'final') {
      diagLogger.log('DIAG-ABSTIME', JSON.stringify({ source: 'final_fallback', t, currentDelay, prevSource: lastSource }))
    }
    lastSource = 'final'
    return t
  }

  function cachedBufferDelay(): number {
    return cachedBufferDelayMs
  }

  function lastAbsTimeSource(): string {
    return lastSource
  }

  function reset() {
    lastVideoAbsTime = 0
    lastVideoAbsTimeTs = 0
    lastSource = ''
  }

  return { offsetToAbsTime, getVideoAbsTime, cachedBufferDelay, lastAbsTimeSource, reset }
}
