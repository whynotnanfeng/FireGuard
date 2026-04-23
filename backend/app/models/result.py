import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.time import now_beijing

if TYPE_CHECKING:
    from app.models.task import Task


class TaskResult(SQLModel, table=True):
    __tablename__ = "results"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    task_id: str = Field(foreign_key="tasks.id", unique=True, index=True)
    result_path: str = Field(default="", max_length=1000)
    detections: str = Field(default="[]", max_length=100000)
    created_at: datetime = Field(default_factory=now_beijing)

    task: Optional["Task"] = Relationship(back_populates="result")
