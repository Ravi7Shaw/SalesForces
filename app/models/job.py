from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(128), index=True)
    # pending | running | paused | interrupted | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    objects_json: Mapped[str] = mapped_column(Text)
    completed_objects_json: Mapped[str] = mapped_column(Text, default="[]")
    current_object: Mapped[str | None] = mapped_column(String(128), nullable=True)
    records_per_object: Mapped[int] = mapped_column(Integer, default=1000)
    processed_objects: Mapped[int] = mapped_column(Integer, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class JobObject(Base):
    """Per-object checkpoint: state, row count, timing, error and landed file for one object in one job."""

    __tablename__ = "job_objects"
    __table_args__ = (UniqueConstraint("job_id", "object_name", name="uq_job_object"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("ingestion_jobs.id"), index=True)
    object_name: Mapped[str] = mapped_column(String(128))
    # Pending | InProgress | JobComplete | Failed   (mirrors Salesforce Bulk job states)
    state: Mapped[str] = mapped_column(String(32), default="Pending")
    salesforce_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    minio_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
