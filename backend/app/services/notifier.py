import json
import logging
from typing import Dict, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)

class Notifier:
    """Broadcasts task status updates to connected WebSocket clients."""
    
    def __init__(self):
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

    async def broadcast_status(self, task_id: str, status: str, message: str = "", force: bool = False):
        """Broadcast a status change, skipping identical consecutive states."""
        if not self._active_connections:
            return

        # Avoid re-sending a state that has not changed.
        state_key = f"{status}:{message}"
        if not force and self._last_states.get(task_id) == state_key:
            return
        
        self._last_states[task_id] = state_key
        # Running updates arrive every second; log only on changes worth seeing.
        if status != "running" or force:
            logger.info(f"[Notifier] Broadcasting update for {task_id}: {status} ({message})")

        payload = json.dumps({
            "type": "task_status_update",
            "task_id": task_id,
            "status": status,
            "message": message
        })
        
        to_remove = []
        for connection in list(self._active_connections):
            try:
                await connection.send_text(payload)
            except Exception as e:
                logger.warning(f"Failed to send notification to client: {e}")
                to_remove.append(connection)
        
        for conn in to_remove:
            self.disconnect(conn)

notifier = Notifier()
