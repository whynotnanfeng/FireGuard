/**
 * HLS 播放器策略：实例创建、错误重试与销毁。
 *
 * 组件只负责业务流程（何时切流、何时进入历史模式），
 * hls.js 的配置与 404 重试策略集中在这里，避免散落在 1.5k 行的组件中。
 */
import Hls from 'hls.js'
import { logger } from '@/utils/logger'

/**
 * 首个分片生成前的等待窗口。
 *
 * 1080p 双光任务要先等编码器启动、模型加载（首次 2–8s）才写出第一片；
 * 10 秒窗口在慢机器上必然误报"视频流初始化失败"。40 秒足够覆盖冷启动，
 * 任务真失败时 WebSocket 会把状态切成 exception，不需要靠这里兜底。
 */
const MAX_MANIFEST_404_RETRIES = 40
const MAX_FRAG_404_RETRIES = 20

export interface HlsErrorOptions {
  taskId: string
  isActive: () => boolean
  /** 清单 404：组件把状态切到 loading。 */
  onManifestRetry: () => void
  /** 分片 404：组件把状态切到 buffering。 */
  onFragmentBuffering: () => void
  /** 重试耗尽：组件进入 exception 并销毁播放器。 */
  onFatal: (message: string) => void
  /** 不可恢复的未知错误：组件只做清理。 */
  onDispose: () => void
}

const HLS_CONFIG = {
  enableWorker: true,
  lowLatencyMode: false,
  backBufferLength: 10,
  maxBufferLength: 20,
  maxMaxBufferLength: 40,
  liveSyncDurationCount: 3.0,
  liveMaxLatencyDurationCount: 6.0,
  maxLiveSyncPlaybackRate: 1.2,
  highBufferWatchdogPeriod: 5,
  manifestLoadingMaxRetry: 10,
  manifestLoadingTimeOut: 5000,
  manifestLoadingRetryDelay: 1000,
  liveDurationInfinity: true,
  startLevel: -1,
  testBandwidth: false,
  progressive: true,
}

export function createHlsForMedia(video: HTMLVideoElement, url: string): Hls {
  const hls = new Hls(HLS_CONFIG)
  hls.loadSource(url)
  hls.attachMedia(video)
  return hls
}

export function bindHlsErrorHandler(hls: Hls, url: string, opts: HlsErrorOptions): void {
  let manifest404RetryCount = 0
  let frag404RetryCount = 0

  hls.on(Hls.Events.ERROR, (_event, data) => {
    if (!data.fatal) {
      if (data.details === Hls.ErrorDetails.FRAG_LOAD_ERROR) {
        logger.debug('hls', 'Non-fatal fragment load error', { taskId: opts.taskId })
      }
      return
    }

    switch (data.type) {
      case Hls.ErrorTypes.NETWORK_ERROR:
        logger.warn('hls', 'Network error', { taskId: opts.taskId, details: data.details })
        if (data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR && data.response?.code === 404) {
          manifest404RetryCount++
          if (manifest404RetryCount > MAX_MANIFEST_404_RETRIES) {
            logger.error('hls', 'Manifest 404 max retries exceeded', {
              taskId: opts.taskId,
              retries: manifest404RetryCount,
            })
            opts.onFatal('视频分片生成超时：请确认视频源有画面，或稍后重新执行任务')
            return
          }
          logger.info('hls', `Manifest 404, retry ${manifest404RetryCount}/${MAX_MANIFEST_404_RETRIES} in 1s`, {
            taskId: opts.taskId,
          })
          opts.onManifestRetry()
          setTimeout(() => {
            if (opts.isActive()) hls.loadSource(url)
          }, 1000)
        } else if (data.details === Hls.ErrorDetails.FRAG_LOAD_ERROR && data.response?.code === 404) {
          frag404RetryCount++
          if (frag404RetryCount > MAX_FRAG_404_RETRIES) {
            logger.error('hls', 'Fragment 404 max retries exceeded, stream is dead', {
              taskId: opts.taskId,
            })
            opts.onFatal('视频推流已意外中断，请检查摄像头或视频源状态')
            return
          }
          logger.info('hls', `Fragment 404, retry ${frag404RetryCount}/${MAX_FRAG_404_RETRIES}`, {
            taskId: opts.taskId,
          })
          opts.onFragmentBuffering()
          setTimeout(() => {
            if (opts.isActive()) hls.startLoad()
          }, 1000)
        } else {
          hls.startLoad()
        }
        break
      case Hls.ErrorTypes.MEDIA_ERROR:
        logger.error('hls', 'Media error', { taskId: opts.taskId, details: data.details })
        hls.recoverMediaError()
        break
      default:
        opts.onDispose()
        break
    }
  })
}

export function destroyHls(hls: Hls | null): null {
  hls?.destroy()
  return null
}
