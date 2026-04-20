# ADR-0002: State-Aware Status Wall for System-Wide Stability

**Date**: 2026-04-20
**Status**: accepted
**Deciders**: AI Assistant, User

## Context

The FireGuard platform experienced a "Refresh Storm" during video stream initialization. This was caused by high-frequency WebSocket status updates being sent to the frontend even when the status text (e.g., "Connecting...") hadn't changed.

The frontend `TaskList.vue` component was configured to reload all task data upon receiving any WebSocket signal, leading to dozens of redundant API calls per second, visual flickering, and UI unresponsiveness.

## Decision

We implement a **State-Aware Status Wall** in the backend `Notifier` service:
1.  **Centralized Filtering**: The `Notifier` (singleton) now maintains a `_last_task_states` dictionary mapping `task_id` to the last broadcasted `(status, message)`.
2.  **Upstream Suppression**: Before a broadcast is sent, the content is compared against the last state. If they are identical, the broadcast is suppressed.
3.  **Frontend Debouncing**: As a secondary defense, the frontend `loadData` function is wrapped in a 500ms debounce timer.

## Alternatives Considered

### Alternative 1: Frontend-Only Filtering
- **Pros**: Zero backend complexity.
- **Cons**: High network traffic; the frontend still has to parse and discard dozens of duplicate messages.
- **Why not**: Does not address the root cause of network noise.

### Alternative 2: Throttled Broadcasting in VideoStream
- **Pros**: Reduces frequency at the source.
- **Cons**: Difficult to manage across different task types (Video, Image, Stream) and components (Sentinel vs. Grabber).
- **Why not**: Lacks the global consistency provided by a centralized `Notifier`.

## Consequences

### Positive
- **Zero Noise**: The UI only refreshes when a meaningful change occurs (e.g., "Connecting" -> "Running").
- **Network Efficiency**: Significant reduction in WebSocket and REST API traffic.
- **Robustness**: The filtering persists regardless of how many background threads or watchdogs try to update the status.

### Negative
- **State Cache**: Small memory overhead in the `Notifier` to store the state dictionary.
- **Forced Syncs**: If a legitimate "quiet" update is needed (without a text change), it might be filtered unless specifically handled.

### Risks
- If the backend crashes and restarts, the `_last_task_states` map is cleared.
- **Mitigation**: The system performs a "Zombie Task Cleanup" and initial sync on startup to reset all states correctly.

---
*Supersedes: None*
