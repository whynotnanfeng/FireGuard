import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.model import DetectionModel
    from app.models.result import Result
    from app.models.user import User


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    user_id: str = Field(foreign_key="users.id", index=True)
    model_id: str = Field(foreign_key="detection_models.id", index=True)
    name: str = Field(max_length=50)
    task_type: str = Field(max_length=20)        # "image" | "video" | "stream"
    input_types: str = Field(max_length=50)      # JSON: ["rgb"] | ["ir"] | ["rgb","ir"]
    source_path: str = Field(default="", max_length=1000)
    source_type: str = Field(max_length=20)      # "upload" | "url" | "rtsp"
    description: str = Field(default="", max_length=2000)
    status: str = Field(default="creating", max_length=20)
    # "creating" | "pending" | "running" | "paused" | "completed" | "failed"
    progress: int = Field(default=0)             # 0-100
    error_msg: str = Field(default="", max_length=1000)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    user: Optional["User"] = Relationship(back_populates="tasks")
    model: Optional["DetectionModel"] = Relationship(back_populates="tasks")
    result: Optional["Result"] = Relationship(back_populates="task")
