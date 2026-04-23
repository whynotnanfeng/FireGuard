import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.time import now_beijing

if TYPE_CHECKING:
    from app.models.model import DetectionModel
    from app.models.result import TaskResult
    from app.models.user import User
    from app.models.detection_record import DetectionRecord


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
    task_type: str = Field(max_length=20)
    input_types: str = Field(max_length=50)
    source_path: str = Field(default="", max_length=1000)
    source_type: str = Field(max_length=20)
    description: str = Field(default="", max_length=2000)
    status: str = Field(default="pending", max_length=20)
    progress: int = Field(default=0)
    error_msg: str = Field(default="", max_length=1000)
    detection_config: str = Field(default="{}", max_length=10000)
    # V12: 是否曾经成功捕获过画面（区分"初始"和"曾暂停"）
    has_history: bool = Field(default=False)
    resolution_width: Optional[int] = Field(default=None)
    resolution_height: Optional[int] = Field(default=None)
    first_session_start_time: Optional[datetime] = Field(default=None)
    session_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=now_beijing)
    updated_at: datetime = Field(default_factory=now_beijing)

    user: Optional["User"] = Relationship(back_populates="tasks")
    model: Optional["DetectionModel"] = Relationship(back_populates="tasks")
    result: Optional["TaskResult"] = Relationship(back_populates="task", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    detection_records: List["DetectionRecord"] = Relationship(back_populates="task", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
