"""
事件驱动检测记录模型

与旧版 DetectionRecord 的区别：
- 旧版：每帧一条记录（高 FPS 下数据爆炸）
- 新版：每个目标生命周期一条记录（Enter → Update × N → Leave）

数据量对比（15 FPS，1 分钟，5 个目标）：
- 旧版：15 × 60 = 900 条/目标 × 5 = 4500 条
- 新版：2 条/目标（Enter + Leave）× 5 = 10 条
- 减少：99.8%
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
