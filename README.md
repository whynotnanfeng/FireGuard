# FireGuard — Intelligent Fire Monitoring System

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/Rust-1.75%2B-orange.svg)](rust/Cargo.toml)
[![Vue](https://img.shields.io/badge/Vue-3.3-42b883.svg)](frontend/package.json)
[![ONNX Runtime](https://img.shields.io/badge/ONNX%20Runtime-DirectML%20%7C%20CUDA-005CED.svg)](https://onnxruntime.ai/)

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

> **v3.0.0 runtime:** production builds ship the Rust core in `rust/` (axum + sqlx + ort)
> as a single self-contained `FireGuard.exe` — no Python runtime is installed.
> `backend/` remains in the repository as the Python reference implementation used for
> algorithm development and side-by-side comparison; it is not part of the installer.

---

## Core Capabilities

### Detection engine

- **Three task types** — `image`, `video`, and `stream`, each configurable with multiple models.
- **Three-stage fusion pipeline** — threshold pre-filter → illumination-aware adaptive
  weighting → Weighted Box Fusion (WBF). Each model keeps its own per-class thresholds.
- **Illumination-aware adaptation** — an EMA-smoothed weight adjustment with cold-start
  repair shifts trust toward the IR model when ambient light drops.
- **In-process inference pool** — each model keeps a small ONNX Runtime session pool
  (GPU default 2, CPU 1) and Rayon workers; no cross-process frame shipping.
- **Runtime selection** — DirectML first (any DX12 GPU), CUDA when the optional runtime
  pack is installed, automatic CPU fallback when GPU initialisation fails.

### Event and recording pipeline

- **Event-driven detection records** — Enter/Leave lifecycle events replace per-frame
  database writes, cutting database I/O by ~99.8%.
- **Server-side annotation rendering** — boxes are drawn in Rust and encoded straight into
  the HLS stream, guaranteeing frame-exact sync and removing frontend canvas complexity.
- **Single annotated HLS channel** — `stream_annotated.m3u8` by default; optional raw feed
  behind `RECORD_RAW_HLS`. Segment windows follow the task's retention hours and are
  rotated by FFmpeg, with orphan cleanup and a disk quota janitor.
- **Media time base** — FFmpeg `showinfo` PTS is normalised by `MediaClock`; records store
  `abs_ms` and `media_pts_ms`, so playback seeks and boxes share one timeline (no NTP/EWMA).
- **Cross-modal alignment** — an RGB/IR freshness window (500 ms) keeps stale IR frames
  out of fusion.
- **Resumable execution** — stopping a stream task keeps segments, playlists and records;
  the next run appends and continues track IDs from the previous maximum.
- **Batch persistence** — detection records are committed by a single writer thread
  (up to 512 rows / 30 ms per transaction); storage and record janitors enforce retention.

### Media gateway

- RTSP / RTMP / HLS ingestion over TCP through an FFmpeg subprocess, with a watchdog that
  restarts capture after a 6-second frame stall.
- Encoder selection: NVENC → QSV → AMF → libx264, all driven by PTS-throttled frame rates.
- A MediaMTX gateway with separated RTSP / HLS / WebRTC ports and lifecycle management.
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
│              Runtime — Rust core (v3.0.0)                                                                          │
│                http://localhost:8000                                                                               │
│  ┌────────────┐   ┌────────────────┐   ┌────────────────┐                                                          │
│  │ fg-app     │   │ FusionEngine   │   │ fg-infer       │                                                          │
│  │ task types │   │ 3-stage fusion │   │ ort CPU / DML  │                                                          │
│  └────────────┘   └────────────────┘   └────────────────┘                                                          │
│  ┌───────────────────┐   ┌─────────────────┐   ┌────────────────┐                                                  │
│  │ fg-media          │   │ fg-infer pool   │   │ Tracker        │                                                  │
│  │ RTSP / RTMP / HLS │   │ session pool    │   │ Enter / Leave  │                                                  │
│  └───────────────────┘   └─────────────────┘   └────────────────┘                                                  │
│  ┌───────────────────┐   ┌────────────────────┐   ┌─────────────────┐                                              │
│  │ HLS writer        │   │ Annotated writer   │   │ fg-server       │                                              │
│  │ clean + resumable │   │ boxes burned in    │   │ axum HTTP/WS    │                                              │
│  └───────────────────┘   └────────────────────┘   └─────────────────┘                                              │
└──────────────┬──────────────────────────────┬─────────────────────────────┬──────────────────────────┬─────────────┘
             │                              │                             │                          │
┌─────────────▼─────────┐   ┌────────────────▼────────────┐   ┌────────────▼────────┐   ┌─────────────▼────────┐
│ SQLite (WAL)          │   │ In-process event bus        │   │ MediaMTX            │   │ Storage              │
│ fg-store repositories │   │ broadcast, no Redis         │   │ RTSP / HLS / WebRTC │   │ segments + artifacts │
└───────────────────────┘   └─────────────────────────────┘   └─────────────────────┘   └──────────────────────┘
```

> The Python reference implementation (`backend/`) mirrors this layout with
> FastAPI + SQLModel and can be useful for algorithm experiments, but the shipped
> product is the Rust runtime above.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Runtime (v3.0.0) | Rust workspace: `fg-domain` / `fg-contracts` / `fg-infer` / `fg-media` / `fg-store` / `fg-app` / `fg-server` |
| Frontend | Vue 3, TypeScript, Vite, Ant Design Vue, Pinia, Vue Router, hls.js |
| Reference backend | Python 3.10+, FastAPI, Uvicorn, Pydantic v2, SQLModel (not shipped) |
| Inference | ONNX Runtime via `ort` (DirectML by default, optional CUDA runtime pack) |
| Streaming | FFmpeg, MediaMTX (RTSP / HLS / WebRTC) |
| Data | SQLite (WAL) through `fg-store` repositories; in-process event bus (no Redis) |
| Auth | JWT + bcrypt, 60-second WebSocket tickets |

Supported detector families: **YOLO**, **RT-DETR**, and custom-trained ONNX models.

---

## Repository Layout

```
.
├── rust/
│   ├── fireguardd/               # entry point: CLI, single instance, Job Object, logging
│   └── crates/
│       ├── fg-domain/            # pure domain models and time helpers
│       ├── fg-contracts/         # config, API DTOs, event payloads
│       ├── fg-infer/             # ort sessions, pre/post-processing, model pool
│       ├── fg-media/             # FFmpeg capture/encode, MediaMTX, HLS writers
│       ├── fg-store/             # sqlx + repositories (task/model/user/record)
│       ├── fg-app/               # engine, task lifecycle, fusion, tracker, janitor
│       └── fg-server/            # axum routes, auth, static assets
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
│   └── requirements-gpu.txt      # CUDA 12.1 dependencies
├── frontend/
│   └── src/
│       ├── api/       # Axios clients
│       ├── components/           # Player, result viewer, detection config
│       ├── composables/          # useHlsPlayer / useDetectionBuffer / useLiveDrift
│       ├── stores/               # Pinia stores
│       ├── utils/                # Logging helpers
│       └── views/                # Login, tasks, models, monitor dashboard
├── packaging/                    # Windows build scripts + Inno Setup scripts
└── simulator/                    # Synthetic RTSP stream generator for testing
```

---

## Quick Start

### Prerequisites

- Python 3.10 or newer (reference backend and simulator only)
- Node.js 18 or newer (frontend build)
- Rust toolchain (production runtime build)
- FFmpeg on `PATH`
- MediaMTX (optional, for the RTSP/HLS/WebRTC gateway)
- Redis only for the Python reference backend (optional; the Rust runtime does not use it)

### 0. Production runtime (Rust, recommended)

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_rust.ps1
# artifacts: dist\FireGuard\FireGuard.exe, dist\FireGuard-Portable.zip,
#            packaging\installer\Output\FireGuard-Setup-3.0.0.exe
```

Run `dist\FireGuard\FireGuard.exe` (double-click opens the browser). It serves the API and
the built frontend on <http://localhost:8000>, stores data in `data\` next to the exe
(portable) or `%ProgramData%\FireGuard\data` (installed), and exits about 15 seconds after
the last console page closes. Use `--keep-alive` to keep it resident.

Quality gates before submitting a change:

```powershell
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
powershell -ExecutionPolicy Bypass -File rust\check-deps.ps1
```

### 1. Reference backend (Python, development only)

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

> The table below documents the Python reference backend. The Rust runtime reads a smaller,
> largely overlapping set: `FIREGUARD_PORT`, `FIREGUARD_DATA_DIR`, `MAX_STORAGE_BYTES`,
> `STREAM_FRAME_WIDTH/HEIGHT`, `DETECTION_FPS_STREAM`, `ANNOTATED_HLS_FPS`, `CAPTURE_FPS`,
> `STREAM_RETENTION_HOURS`, `STORAGE_JANITOR_INTERVAL`, `MAX_STREAM_TASKS`,
> `MAX_TOTAL_WORKERS`, `INFERENCE_POOL_SIZE`, `DETECTION_RECORD_FPS`, `USE_GPU`,
> `RECORD_RAW_HLS`, `MEDIAMTX_*_PORT`. See `CLAUDE.md` → Environment for details.

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
| API + frontend | 8000 |
| Frontend dev server | 5173 |
| Redis (Python reference only) | 6379 |
| MediaMTX RTSP (backend) | 8554 |
| MediaMTX HLS | 8888 |
| MediaMTX WebRTC | 8889 |
| MediaMTX control API (backend) | 9997 |
| Stream simulator API | 8001 |
| MediaMTX RTSP (simulator) | 8555 |
| MediaMTX control API (simulator) | 9996 |

### Start everything (three terminals)

| Terminal | Working directory | Command | URL |
|----------|-------------------|---------|-----|
| 1 | `backend` | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` | <http://localhost:8000/api/docs> |
| 2 | `frontend` | `npm run dev` | <http://localhost:5173> |
| 3 | `simulator` | `uvicorn main:app --host 0.0.0.0 --port 8001` | <http://localhost:8001> |

On Windows, run the backend with `run_backend.bat` inside `backend/` — it finds a local
`.venv`, `venv` or `env` automatically and falls back to the `python` on `PATH`, so no
absolute interpreter path is needed.

The simulator is optional — only start it if you have no physical camera available.

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
| `POST` | `/api/tasks/{id}/execute` | Start a task |
| `POST` | `/api/tasks/{id}/stop` | Stop a task |
| `GET` | `/api/tasks/{id}/detection-events` | Detection events for a task |
| `WS` | `/ws/notifications?ticket=` | Real-time notification channel |
| `WS` | `/ws/stream/{task_id}?ticket=` | Live annotated stream session |

WebSocket connections need a short-lived ticket from `POST /api/auth/ws-ticket`.
The detailed endpoint reference lives in [`docs/api.md`](docs/api.md).

---

## Streaming Setup

Place the `mediamtx` binary where the service can find it, then adjust the gateway
settings (API port `9997`, RTSP `8554`, HLS `8888` are the defaults).

Camera sources are configured per task and may be:

| Source | Type | Notes |
|--------|------|-------|
| RTSP camera | `rtsp` | Standard IP cameras (Hikvision, Dahua, Uniview…), TCP transport |
| ONVIF device | — | Built-in WS-Discovery in the Rust core returns the RTSP URL |
| Drone downlink | `rtsp` / `url` | UAVs such as DJI Matrice or Autel EVO pushing RTSP/RTMP |
| Video file upload | `upload` | Local file, transcoded into an RTSP stream |
| HTTP / RTMP stream | `url` | Any HTTP or RTMP source |

For RGB + IR fusion, register both stream URLs; the core keeps a 500 ms freshness window
so stale IR frames are excluded from fusion.

---

## Stream Simulator

`simulator/` contains a lightweight synthetic stream generator, useful for development
when no physical camera is available. It publishes uploaded video files over RTSP and
exposes a small dashboard for monitoring stream health.

```bash
cd simulator

python -m venv .venv
# Windows:   .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt

# Windows
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001
# Linux/macOS
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

The dashboard is then available at <http://localhost:8001>.

The simulator starts its own media gateway on RTSP `8555` / API `9996` (deliberately
offset from the backend gateway on `8554` / `9997`, so both can run side by side). It
also needs FFmpeg and a `mediamtx` binary on `PATH` (or under `bin/`).

---

## License

Released under the [MIT License](LICENSE).

---

## Acknowledgements

Built on the shoulders of excellent open-source projects, including Rust (axum, sqlx, ort),
Vue 3, ONNX Runtime, FFmpeg, MediaMTX and SQLite. The Python reference backend additionally
uses FastAPI. Detector weights are **not** included in this repository — supply your own
trained or appropriately licensed ONNX models.
