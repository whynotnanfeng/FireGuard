import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.time import now_beijing

if TYPE_CHECKING:
    from app.models.model import DetectionModel
    from app.models.task import Task


class TaskModel(SQLModel, table=True):
    """Task-model association table: supports multi-model task configuration.

    Each record represents one model within a task and its fusion configuration.
    Single-model tasks stay compatible through Task.model_id and do not use this table.
    """

    __tablename__ = "task_models"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    task_id: str = Field(foreign_key="tasks.id", index=True)
    model_id: str = Field(foreign_key="detection_models.id")
    weight: float = Field(default=1.0)  # Fusion weight
    per_class_thresholds: str = Field(default="{}")  # JSON: {class_name: threshold}
    enabled_classes: str = Field(default="[]")  # JSON: [class_name, ...]
    order_index: int = Field(default=0)
    created_at: datetime = Field(default_factory=now_beijing)

    task: Optional["Task"] = Relationship(back_populates="task_models")
    model: Optional["DetectionModel"] = Relationship()
