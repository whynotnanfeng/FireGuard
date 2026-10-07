# FireGuard — Intelligent Fire Monitoring System

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](backend/requirements.txt)
[![Vue](https://img.shields.io/badge/Vue-3.3-42b883.svg)](frontend/package.json)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688.svg)](backend/requirements.txt)

**FireGuard** is an AI-based, multi-model multimodal fire and smoke detection platform.
It runs deep-learning object detectors over images, video files, and live video streams,
and serves the results through a web console for task management, model management,
live monitoring, and detection-event forensics.

This repository is the English-language open-source release of **FireGuard Intelligent
Fire Monitoring System V1.0**, the name under which the project is registered for
software copyright (registered pinyin name: *Huozai Renzhi Jiance Xitong*).

---

## Table of Contents

- [Why FireGuard](#why-fireguard)
- [Core Capabilities](#core-capabilities)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Repository Layout](#repository-layout)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Detection Models](#detection-models)
- [API Overview](#api-overview)
- [Streaming Setup](#streaming-setup)
- [Stream Simulator](#stream-simulator)
- [License](#license)
- [Acknowledgements](#acknowledgements)

---

## Why FireGuard

Industrial production, warehousing and logistics, commercial buildings, forests and
grasslands all face fire as one of the most destructive safety hazards. Conventional
fire monitoring relies on hardware sensors such as smoke and temperature detectors,
which have limited coverage, a high false-alarm rate, and offer no way to visually
confirm an alarm.

Computer-vision-based detection changes that: it identifies flames and smoke directly
in camera footage, giving earlier warning and evidence a human can actually review.

FireGuard targets five core requirements:

| # | Requirement | How FireGuard meets it |
|---|-------------|------------------------|
| 1 | **Multi-model fusion detection** | Load several ONNX detectors per task and fuse their output to reduce misses and false positives |
| 2 | **Multimodal input** | RGB and IR (thermal) channels with illumination-aware weighting for low-light and night operation |
| 3 | **Live video monitoring** | Low-latency HLS/WebRTC delivery with server-rendered bounding boxes overlaid on the picture |
| 4 | **Playback and resume** | Review past detections and query the event timeline; stream tasks resume from their last checkpoint |
| 5 | **Elastic inference** | A worker pool that scales its process count against live CPU load |

---

## Core Capabilities

### Detection engine

- **Three task types** — `image`, `video`, and `stream`, each configurable with multiple models.
- **Three-stage fusion pipeline** — threshold pre-filter → illumination-aware adaptive
  weighting → Weighted Box Fusion (WBF). Each model keeps its own per-class thresholds.
- **Illumination-aware adaptation** — an EMA-smoothed weight adjustment with cold-start
  repair shifts trust toward the IR model when ambient light drops.
- **Elastic worker pool** — three-level CPU-driven scaling
  (`GREEN` performance / `BALANCED` / `RED` survival), one worker process per model.
- **Zero-copy frame transport** — a shared-memory triple buffer moves RGB/IR frames to
  workers without a JPEG encode/decode round trip (~15 ms/frame saved).
- **Runtime selection** — ONNX Runtime on CPU or NVIDIA CUDA, with GPU availability
  probed at startup.

### Event and recording pipeline

- **Event-driven detection records** — Enter/Leave lifecycle events replace per-frame
  database writes, cutting database I/O by ~99.8%.
- **Server-side annotation rendering** — OpenCV draws the boxes server-side and encodes
  them straight into the HLS stream, guaranteeing frame-exact sync and removing frontend
  canvas complexity.
- **Dual HLS pipeline** — `DirectHLSWriter` (clean feed) plus `AnnotatedHLSWriter`
  (annotated feed), with resumable segments, wall-clock frame-rate control, and automatic
  FFmpeg restart.
- **NTP-grade clock sync** — EWMA calibration with outlier rejection and HLS
  `programDateTime` integration aligns playback position with detection events.
- **Cross-modal alignment** — an RGB/IR pairing buffer keyed on wall-clock time with a
  50 ms tolerance.
- **Resumable execution** — HLS segments and m3u8 playlists let a stopped stream task
  continue from its checkpoint instead of recomputing.
- **Multi-layer caching** — directory listings (30 s TTL), live m3u8 (1 s TTL) and
  metadata (5 s TTL) cut API response time sharply.

### Media gateway

- RTSP / RTMP / HLS ingestion over TCP with an 8 MB buffer and exponential-backoff reconnect.
- FFmpeg-based capture with CUDA / Intel QSV / AMD AMF hardware decoding and adaptive
  frame-rate control (PI controller plus CPU-aware throttling).
- A MediaMTX gateway with fully separated RTSP / HLS / WebRTC ports, dynamic stream path
  registration, and lifecycle management.
- ONVIF device discovery for IP cameras.

### Web console

Vue 3 + TypeScript + Ant Design Vue, covering authentication, model management, task
management, multi-channel live monitoring with a dashboard grid, historical playback,
detection-event tracking, and storage/log diagnostics.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│             Frontend — Vue 3 + TypeScript                                                                          │
│                  http://localhost:5173                                                                             │
└──────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────┘
                                                           │  REST /api · WebSocket /ws · HLS /storage
┌──────────────────────────────────────────────────────────▼─────────────────────────────────────────────────────────┐
│              Backend — FastAPI (Python)                                                                            │
│                http://localhost:8000                                                                               │
│  ┌────────────┐   ┌────────────────┐   ┌────────────────┐                                                          │
│  │ TaskRunner │   │ FusionEngine   │   │ ONNX Inference │                                                          │
│  │ task types │   │ 3-stage fusion │   │ CPU / CUDA     │                                                          │
│  └────────────┘   └────────────────┘   └────────────────┘                                                          │
│  ┌───────────────────┐   ┌─────────────────┐   ┌────────────────┐                                                  │
│  │ FFmpegCapture     │   │ Worker Pool     │   │ EventProcessor │                                                  │
│  │ RTSP / RTMP / HLS │   │ elastic scaling │   │ Enter / Leave  │                                                  │
│  └───────────────────┘   └─────────────────┘   └────────────────┘                                                  │
│  ┌───────────────────┐   ┌────────────────────┐   ┌─────────────────┐                                              │
│  │ DirectHLSWriter   │   │ AnnotatedHLSWriter │   │ ONVIF / RGBT    │                                              │
│  │ clean + resumable │   │ boxes burned in    │   │ frame alignment │                                              │
│  └───────────────────┘   └────────────────────┘   └─────────────────┘                                              │
└──────────────┬──────────────────────────────┬─────────────────────────────┬──────────────────────────┬─────────────┘
             │                              │                             │                          │
┌─────────────▼─────────┐   ┌────────────────▼────────────┐   ┌────────────▼────────┐   ┌─────────────▼────────┐
│ SQLite (WAL)          │   │ Redis                       │   │ MediaMTX            │   │ Storage              │
│ tasks, models, events │   │ config / registry / pub-sub │   │ RTSP / HLS / WebRTC │   │ segments + artifacts │
└───────────────────────┘   └─────────────────────────────┘   └─────────────────────┘   └──────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Vue 3, TypeScript, Vite, Ant Design Vue, Pinia, Vue Router, hls.js |
| Backend | Python 3.10+, FastAPI, Uvicorn, Pydantic v2, SQLModel |
| Inference | ONNX Runtime (CPU or CUDA 12.1), OpenCV, NumPy |
| Streaming | FFmpeg, MediaMTX (RTSP / HLS / WebRTC) |
| Data | SQLite (WAL), Redis (config, registry, pub/sub, in-memory fallback) |
| Auth | JWT via `python-jose`, bcrypt via `passlib`, rate limiting via SlowAPI |

Supported detector families: **YOLO**, **RT-DETR**, and custom-trained ONNX models.

---

## Repository Layout

```
.
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI application entry point
│   │   ├── config.py             # Settings and runtime configuration
│   │   ├── dependencies.py       # Auth and database dependencies
│   │   ├── limiter.py            # Rate limiting
│   │   ├── models/               # SQLModel / Pydantic schemas
│   │   ├── repositories/         # Data access layer
│   │   ├── routers/              # auth, models, tasks
│   │   ├── schemas/              # Request / response schemas
│   │   ├── services/             # Detection, fusion, streaming, storage
│   │   └── utils/                # Logging, clocks, shared memory, hardware
│   ├── tests/                    # Test suite
│   ├── migrate_db.py             # Schema migration helper
│   ├── requirements.txt          # CPU dependencies
│   ├── requirements-gpu.txt      # CUDA 12.1 dependencies
│   └── requirements-dev.txt      # Development dependencies
├── frontend/
│   └── src/
│       ├── api/       # Axios clients
│       ├── components/           # Player, result viewer, detection config
│       ├── stores/               # Pinia stores
│       ├── utils/                # Logging helpers
│       └── views/                # Login, tasks, models, monitor dashboard
└── simulator/                    # Synthetic RTSP stream generator for testing
```

---

## Quick Start

### Prerequisites

- Python 3.10 or newer
- Node.js 18 or newer
- FFmpeg on `PATH`
- MediaMTX (optional, for the RTSP/HLS/WebRTC gateway)
- Redis (optional — falls back to an in-memory broker)

### 1. Backend

```bash
cd backend

python -m venv .venv
# Windows:   .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt          # CPU
# pip install -r requirements-gpu.txt    # NVIDIA CUDA 12.1

cp .env.example .env                     # then edit SECRET_KEY
```

Generate a JWT secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Start the API server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive API documentation is then available at <http://localhost:8000/api/docs>
(development mode only — it is disabled when `FIREGUARD_MODE=production`).

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

The console is served at <http://localhost:5173> and proxies `/api`, `/ws` and
`/storage` to the backend.

### 3. Production build

```bash
cd frontend
npm run build      # output in frontend/dist
```

---

## Configuration

All settings come from environment variables; see [`backend/.env.example`](backend/.env.example).

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | — | **Required.** JWT signing secret |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Access token lifetime |
| `MAX_STORAGE_BYTES` | `1073741824` | Total storage quota (1 GiB) |
| `MAX_FILE_SIZE_BYTES` | `1073741824` | Per-file size limit |
| `MAX_UPLOAD_SIZE_MB` | `1024` | Upload size limit |
| `MAX_CONCURRENT_TASKS` | `1` | Concurrent task limit |
| `STREAM_FPS` | `30` | Stream processing frame rate |
| `RECONNECT_MAX_RETRIES` | `3` | Stream reconnect attempts |
| `USE_FFMPEG_CAPTURE` | `True` | Use FFmpeg instead of OpenCV for capture |
| `ALLOWED_ORIGINS` | localhost dev origins | CORS allow-list |
| `REDIS_URL` | in-memory fallback | Redis connection string |

Never commit a populated `.env`; it is excluded from version control.

### Port map

| Service | Port |
|---------|------|
| Backend API | 8000 |
| Frontend dev server | 5173 |
| Redis | 6379 |
| MediaMTX RTSP | 8554 |
| MediaMTX HLS | 8888 |
| MediaMTX WebRTC | 8889 |
| MediaMTX control API | 9997 |
| Stream simulator | 8001 |

---

## Detection Models

FireGuard runs standard ONNX object detectors. Export your model to ONNX, then register
it in the console under **Model Management**.

Expected output contract:

- **Input** — one image tensor, typically `[1, 3, H, W]`, normalised to `0..1` with
  `RGB` channel order.
- **Output** — `[1, N, 5 + num_classes]`, laid out as
  `[cx, cy, w, h, objectness, class scores...]` in **input-image pixel coordinates**.

Compatible architectures include YOLO (v5/v8-style exports) and RT-DETR. Custom-trained
models work as long as they follow the layout above. Label names are mapped to display
names through the label-mapping editor in the console.

GPU execution requires an NVIDIA driver (>= 525.60) and CUDA Toolkit 12.1. FFmpeg
hardware decoding is tried in the order CUDA (NVDEC) → Intel QSV → AMD AMF → CPU.

---

## API Overview

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/login` | Authenticate and obtain a JWT |
| `GET` | `/api/models` | List registered detection models |
| `POST` | `/api/models` | Register a model |
| `GET` | `/api/tasks` | List detection tasks |
| `POST` | `/api/tasks` | Create an `image` / `video` / `stream` task |
| `POST` | `/api/tasks/{id}/start` | Start a task |
| `POST` | `/api/tasks/{id}/stop` | Stop a task |
| `GET` | `/api/tasks/{id}/events` | Detection events for a task |
| `WS` | `/ws/notifications` | Real-time notification channel |
| `WS` | `/ws/stream/{task_id}` | Live annotated stream session |

Full request/response schemas are generated from the running service at `/api/docs`.

---

## Streaming Setup

Place the `mediamtx` binary where the service can find it, then adjust the gateway
settings (API port `9997`, RTSP `8554`, HLS `8888` are the defaults).

Camera sources are configured per task and may be:

| Source | Type | Notes |
|--------|------|-------|
| RTSP camera | `rtsp` | Standard IP cameras (Hikvision, Dahua, Uniview…), TCP transport |
| ONVIF device | — | Discovered automatically via `onvif_client.py`, which returns the RTSP URL |
| Drone downlink | `rtsp` / `url` | UAVs such as DJI Matrice or Autel EVO pushing RTSP/RTMP |
| Video file upload | `upload` | Local file, transcoded into an RTSP stream |
| HTTP / RTMP stream | `url` | Any HTTP or RTMP source |

For RGB + IR fusion, register both stream URLs; the alignment buffer pairs frames by
wall-clock time within a 50 ms tolerance.

---

## Stream Simulator

`simulator/` contains a lightweight synthetic stream generator, useful for development
when no physical camera is available.

```bash
cd simulator
pip install -r requirements.txt
python main.py
```

It publishes test patterns over RTSP and exposes a small dashboard for monitoring
stream health.

---

## License

Released under the [MIT License](LICENSE).

---

## Acknowledgements

Built on the shoulders of excellent open-source projects, including FastAPI, Vue 3,
ONNX Runtime, FFmpeg, MediaMTX, SQLite and Redis. Detector weights are **not** included
in this repository — supply your own trained or appropriately licensed ONNX models.