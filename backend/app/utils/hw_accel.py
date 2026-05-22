import subprocess
import logging

logger = logging.getLogger(__name__)

_cached_encoders: dict | None = None


def detect_hw_encoders(ffmpeg_path: str) -> dict:
    """检测FFmpeg支持的所有硬件编码器，结果缓存"""
    global _cached_encoders
    if _cached_encoders is not None:
        return _cached_encoders

    result = {"nvenc": False, "qsv": False, "amf": False, "vaapi": False}
    try:
        cmd = [ffmpeg_path, "-hide_banner", "-encoders"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        output = proc.stdout
        result["nvenc"] = "h264_nvenc" in output
        result["qsv"] = "h264_qsv" in output
        result["amf"] = "h264_amf" in output
        result["vaapi"] = "h264_vaapi" in output
    except Exception as e:
        logger.warning(f"[HWAccel] Encoder detection failed: {e}")

    _cached_encoders = result
    logger.info(f"[HWAccel] Detected hardware encoders: {result}")
    return result


def resolve_hls_encoder(ffmpeg_path: str, priority: list[str] | None = None) -> tuple[str, bool]:
    """
    为HLS录制选择最优编码器。

    Returns:
        (encoder_name, is_hw): 编码器名称和是否为硬件编码
    """
    encoders = detect_hw_encoders(ffmpeg_path)
    if priority is None:
        priority = ["nvenc", "qsv", "amf"]

    encoder_map = {
        "nvenc": ("h264_nvenc", True),
        "qsv": ("h264_qsv", True),
        "amf": ("h264_amf", True),
    }

    for hw_name in priority:
        if encoders.get(hw_name):
            encoder_name, is_hw = encoder_map[hw_name]
            logger.info(f"[HWAccel] HLS encoder resolved: {encoder_name}")
            return encoder_name, is_hw

    logger.critical("[HWAccel] No hardware encoder (NVENC/QSV/AMF) detected. "
                     "Hardware encoding is required for real-time HLS streaming.")
    raise RuntimeError(
        "No hardware encoder available. DetPlatform requires NVIDIA NVENC, "
        "Intel QuickSync (QSV), or AMD AMF for real-time HLS encoding. "
        "Please install a supported GPU or enable GPU passthrough."
    )


def build_hls_encode_args(encoder: str, is_hw: bool, fps: float = 15.0, hls_time: int = 1) -> list[str]:
    """
    根据编码器类型构建FFmpeg编码参数。

    注意：copy模式不需要这些参数，此函数仅在copy失败时使用。
    GOP = fps × hls_time，确保每个 HLS 分段以关键帧起始。
    """
    args = ["-c:v", encoder]
    gop = int(fps * hls_time)

    if encoder == "copy":
        return args

    if is_hw:
        args += [
            "-g", str(gop),
            "-pix_fmt", "nv12",
            "-bf", "0",
        ]
        if "nvenc" in encoder:
            args += [
                "-preset", "p1",
                "-tune", "ll",
                "-rc", "cbr",
                "-b:v", "2M",
                "-maxrate", "2M",
                "-bufsize", "2M",
                "-gpu", "0",
            ]
        elif "qsv" in encoder:
            args += ["-preset", "veryfast", "-b:v", "3M"]
    else:
        args += [
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "100",
            "-pix_fmt", "yuv420p",
            "-bf", "0",
            "-x264-params", f"no-scenecut=1:keyint={gop}:min-keyint={gop}",
        ]

    return args
