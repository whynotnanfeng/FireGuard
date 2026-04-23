import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.time import now_beijing

if TYPE_CHECKING:
    from app.models.task import Task


class DetectionRecord(SQLModel, table=True):
    __tablename__ = "detection_records"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    task_id: str = Field(foreign_key="tasks.id", index=True)
    class_name: str = Field(max_length=50)
    confidence: float = Field()
    box: str = Field(default="[]", max_length=200)
    detected_at: datetime = Field(default_factory=now_beijing)

    task: Optional["Task"] = Relationship(back_populates="detection_records")
