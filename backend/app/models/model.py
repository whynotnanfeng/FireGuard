import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.time import now_beijing

if TYPE_CHECKING:
    from app.models.task import Task
    from app.models.user import User


class DetectionModel(SQLModel, table=True):
    __tablename__ = "detection_models"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    user_id: str = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=100)
    format: str = Field(max_length=10)
    input_types: str = Field(max_length=50)
    file_path: str = Field(max_length=500)
    description: str = Field(default="", max_length=500)
    status: str = Field(max_length=20)
    label_config: Optional[str] = Field(default=None)
    default_threshold: float = Field(default=0.25)
    class_names: str = Field(default="[]", max_length=5000)
    created_at: datetime = Field(default_factory=now_beijing)

    user: Optional["User"] = Relationship(back_populates="detection_models")
    tasks: List["Task"] = Relationship(back_populates="model")
