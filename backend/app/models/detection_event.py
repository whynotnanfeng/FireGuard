"""Event-driven detection records: one row per object lifecycle instead of one per frame.

Per-frame rows grow linearly with FPS; enter/leave events keep the same information at a
small fraction of the volume.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.time import now_beijing

if TYPE_CHECKING:
    from app.models.task import Task


class DetectionEvent(SQLModel, table=True):
    __tablename__ = "detection_events"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    task_id: str = Field(foreign_key="tasks.id", index=True)
    track_id: int = Field(index=True)

    event_type: str = Field(max_length=20, index=True)

    class_name: str = Field(max_length=50, index=True)
    confidence: float = Field()

    box: str = Field(default="[]", max_length=200)

    entered_at: datetime = Field(default_factory=now_beijing, index=True)
    left_at: Optional[datetime] = Field(default=None, index=True)
    duration_ms: int = Field(default=0)

    max_confidence: float = Field(default=0.0)
    avg_confidence: float = Field(default=0.0)
    update_count: int = Field(default=0)

    task: Optional["Task"] = Relationship(back_populates="detection_events")
