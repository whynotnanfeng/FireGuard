# Changelog

All notable changes to the FireGuard project will be documented in this file.

## [v1.2.5] - 2026-04-23

### Added
- **Session-Based HLS Recording**: 
  - Backend: `StorageManager` now supports session-based recording. Each task start/stop cycle creates a new session directory (`session_N/`), with the current live session in `live/`.
  - Backend: Added `generate_merged_m3u8(task_id, force_vod=False)` that concatenates all sessions with `#EXT-X-DISCONTINUITY` markers for seamless cross-session playback.
  - Backend: Added `mode` parameter to `GET /api/tasks/{id}/stream.m3u8` endpoint. `mode=vod` forces `#EXT-X-ENDLIST` for seekable VOD playback; `mode=live` returns an empty playlist fallback when no data is available.
  - Backend: Added `first_session_start_time` and `session_count` fields to the Task model for precise time alignment.
- **Dual-URL HLS Architecture**:
  - Frontend: `initLiveHls()` now uses the direct FFmpeg m3u8 at `/api/storage/{id}/live/index.m3u8` for real-time monitoring, eliminating the white-screen issue caused by empty merged playlists.
  - Frontend: `initVodHls()` uses the merged m3u8 with `?mode=vod` parameter for historical playback with full seek support.
- **Elapsed-Time Progress Bar**:
  - Frontend: Live mode progress bar now uses elapsed time since `first_session_start_time` instead of `video.duration` (which is `Infinity` for live streams), ensuring the slider always stays at the rightmost position.
  - Frontend: Added `startLiveProgressTimer()` for 500ms periodic progress updates in live mode.

### Fixed
- **White Screen on Live Monitoring**: Resolved by using direct FFmpeg m3u8 URL instead of the merged playlist endpoint for live mode.
- **Black Screen on Progress Bar Drag**: Resolved by adding `mode=vod` parameter that forces `#EXT-X-ENDLIST` in the m3u8 manifest, enabling hls.js seek functionality.
- **Black Screen After Task Stop**: Same root cause as above — VOD mode now correctly generates a seekable playlist with ENDLIST.
- **Progress Bar Not at Rightmost Position**: Fixed by replacing `video.duration`-based positioning with elapsed time calculation for live streams.
- **Overlay Not Hiding**: Added `liveVideoReady` flag to properly hide the loading overlay once HLS video starts playing.

### Changed
- **StorageManager Refactoring**: `stop_recording()` now moves `live/` to `session_N/` and finalizes session metadata, enabling proper multi-session concatenation.
- **HLS Error Recovery**: Network errors in live mode now retry with a 2-second delay instead of immediately, reducing error storms.

## [v1.2.4] - 2026-04-22

### Added
- **HLS-Live Hybrid Playback**:
  - Implemented background HLS synchronization even in live mode, allowing instantaneous switching from real-time to historical monitoring.
  - Added cache-busting logic (`?t=timestamp`) to HLS manifests to prevent stale video playback after task restarts.
- **Precision Time Synchronization**:
  - Replaced simulated wall-clock time with real video duration for the progress bar, eliminating "time jumping" and "future seeking" bugs.
  - Improved absolute-to-relative time mapping by using the first actual detection record as the playback epoch.

### Fixed
- **Playback UI Issues**: 
  - Fixed "Invisible Detection Boxes" bug where boxes wouldn't appear on auto-play until manual seeking.
  - Standardized duration formatting in the seekbar to remove floating-point artifacts (e.g., `01:05.123` -> `01:05`).
  - Resolved `tickerInterval` reference error in `VideoPlayer.vue`.
- **Workspace Cleanup**:
  - Removed orphaned `VideoPlayer.backup.vue` to prevent IDE symbol conflicts.

## [v1.2.3] - 2026-04-21

### Added
- **Real-time Model Analysis**: 
  - Backend: Added `POST /api/models/analyze` for on-the-fly metadata extraction from ONNX files.
  - Frontend: Integrated automatic label extraction as soon as an ONNX file is selected in the UI.
- **Database Infrastructure**:
  - **Testing Isolation**: Implemented a dedicated `test.db` and separate `test_engine` in `conftest.py` to prevent regression tests from overwriting development data.
  - **Disaster Recovery**: Added `recover_database.py` utility script to reconstruct schema and model records from filesystem assets.
- **UI Ergonomics**:
  - **Scrollbar Support**: Added vertical scrolling to the label mapping table in `LabelMappingEditor.vue` to handle large numbers of classes (e.g., COCO's 80 classes).
  - **Modal Stability**: Set fixed `550px` maximum height for model management modals to prevent screen overflow.

### Changed
- **Model Upload Flow**:
  - Frontend: The upload modal now automatically resets its state (form fields and files) upon closure.
  - Backend: Removed automatic label overwriting in `POST /api/models` to prioritize user-reviewed mappings from the UI.
- **UI Aesthetics**: Improved modal positioning and spacing for a more premium, balanced feel.

### Fixed
- **Development Data Loss**: Prevented accidental database wipes by isolating the `pytest` database session.
- **Stale Form Data**: Resolved issue where unsubmitted model information persisted when re-opening the upload modal.

---

## [v1.2.2] - 2026-04-20

### Added
- **FPS Throttle Logic**: Accurate millisecond-level throttling for real-time streams.
- **Dynamic Sampling**: Frame stepping for offline video tasks to increase throughput.

---

## [v1.2.1] - 2026-04-19

### Added
- DetectionConfig component for per-category thresholding.
- Real-time detection records with database persistence.
- "Stability Edition" core protocol (5-3 Isolation).
