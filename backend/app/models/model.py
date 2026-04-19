import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlmodel import Field, Relationship, SQLModel

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
    format: str = Field(max_length=10)           # "pt" | "onnx"
    input_types: str = Field(max_length=50)      # JSON: ["rgb"] | ["ir"] | ["rgb","ir"]
    file_path: str = Field(max_length=500)
    description: str = Field(default="", max_length=500)
    status: str = Field(max_length=20)           # "creating" | "completed" | "failed"
    label_config: Optional[str] = Field(default=None)  # JSON-serialized mapping: {"0": "smoke", "1": "fire"}
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    user: Optional["User"] = Relationship(back_populates="detection_models")
    tasks: List["Task"] = Relationship(back_populates="model")
