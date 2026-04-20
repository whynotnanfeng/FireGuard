# ADR-0001: 5-3 Physical Isolation Protocol

**Date**: 2026-04-20
**Status**: accepted
**Deciders**: AI Assistant, User

## Context

In production environments, video streaming (RTSP/HTTP via OpenCV) often encounters network jitter or temporary source drops. OpenCV's `VideoCapture.read()` can block for several seconds during these events, causing:
1.  **UI Feedback Gap**: The user sees a frozen frame with no indication of reconnecting.
2.  **Connection Jitter**: Immediate retry attempts often fail, leading to rapid retry count exhaustion.
3.  **Thread Blocking**: The main event loop or background grabbers becoming unresponsive.

## Decision

We implement the **5-3 Physical Isolation Protocol** for all video streams:
1.  **5s Initial Grace Period**: On start, the system provides a 5.0s window where the status is forced to "Connecting" regardless of background retries.
2.  **3s Rhythmic Retry**: Retries are paced at 3-second intervals to allow the networking stack to clear.
3.  **Sentinel Ticker**: A dedicated heartbeat thread ensures the UI receives "Connecting" updates even if the primary grabber thread is blocked by `cv2.VideoCapture`.
4.  **Watchdog Sync**: The UI lagging watchdog (1.5s threshold) is suppressed during the first 5 seconds to avoid premature "Retry (1/5)" states.

## Alternatives Considered

### Alternative 1: Immediate Retry
- **Pros**: Fastest possible reconnection.
- **Cons**: Causes extreme CPU/Network spike; exhausting 5 retries in <1s.
- **Why not**: Not stable for production RTSP.

### Alternative 2: Long Timeout in OpenCV
- **Pros**: Native implementation.
- **Cons**: `CAP_PROP_TIMEOUT` is inconsistent across platforms/versions.
- **Why not**: Unreliable behavior.

## Consequences

### Positive
- Eliminated state jitter in the UI during startup.
- Robust reconnection for unstable cellular/satellite links.
- Consistent user feedback ("Connecting..." vs "Retry n/5").

### Negative
- Connection failures now take a minimum of 5 seconds to be reported to the user.
- Slight increase in thread overhead due to the Sentinel Ticker.

### Risks
- If `cv2.VideoCapture` blocks for > 20s, the Sentinel (lasting 5s) will stop, but the grace period in the UI might have ended.
- **Mitigation**: The `run` loop watchdog handles long-term lag after the 5s grace period.
