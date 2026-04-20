import json
import logging
from typing import List, Dict, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)

class Notifier:
    """Global WebSocket notification manager for real-time task status updates."""
    
    def __init__(self):
        # Set of active notification WebSockets
        self._active_connections: Set[WebSocket] = set()
        self._last_states: Dict[str, str] = {} # task_id -> "status:message"

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self._active_connections.add(websocket)
        logger.info(f"Notification client connected. Total clients: {len(self._active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self._active_connections:
            self._active_connections.remove(websocket)
            logger.info(f"Notification client disconnected. Remaining: {len(self._active_connections)}")

    async def broadcast_status(self, task_id: str, status: str, message: str = ""):
        """Broadcast status change to all connected clients (Filtered)."""
        if not self._active_connections:
            return

        # V45: Global State Wall - Only broadcast if CONTENT truly changes
        state_key = f"{status}:{message}"
        if self._last_states.get(task_id) == state_key:
            return
        
        self._last_states[task_id] = state_key
        logger.info(f"[Notifier] Broadcasting update for {task_id}: {status} ({message})")

        payload = json.dumps({
            "type": "task_status_update",
            "task_id": task_id,
            "status": status,
            "message": message
        })
        
        # Prepare cleanup list if send fails
        to_remove = []
        for connection in list(self._active_connections):
            try:
                await connection.send_text(payload)
            except Exception as e:
                logger.warning(f"Failed to send notification to client: {e}")
                to_remove.append(connection)
        
        for conn in to_remove:
            self.disconnect(conn)

# Singleton instance
notifier = Notifier()
