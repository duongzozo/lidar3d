"""SQLAlchemy ORM models with PostGIS geometry support."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ── Enums ─────────────────────────────────────────────────────────────────

class DatasetStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


# ── Models ────────────────────────────────────────────────────────────────

class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.VIEWER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    datasets: Mapped[list[Dataset]] = relationship("Dataset", back_populates="owner")
    upload_sessions: Mapped[list[UploadSession]] = relationship(
        "UploadSession", back_populates="user"
    )


class Dataset(TimestampMixin, Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[DatasetStatus] = mapped_column(
        Enum(DatasetStatus), default=DatasetStatus.PENDING, nullable=False, index=True
    )
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # File info
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    file_hash: Mapped[Optional[str]] = mapped_column(String(64))  # SHA256
    file_path: Mapped[Optional[str]] = mapped_column(Text)  # absolute path to merged file

    # Spatial metadata
    bbox_geom = Column(Geometry("POLYGON", srid=4326))  # 2D bounding box
    bbox_3d = Column(JSONB)  # {xmin, ymin, zmin, xmax, ymax, zmax}
    center_lon: Mapped[Optional[float]] = mapped_column(Float)
    center_lat: Mapped[Optional[float]] = mapped_column(Float)
    center_elevation: Mapped[Optional[float]] = mapped_column(Float)

    # Point cloud metadata
    point_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    source_crs: Mapped[Optional[str]] = mapped_column(String(100))  # e.g. "EPSG:32648"
    output_crs: Mapped[str] = mapped_column(String(20), default="EPSG:4326")
    elevation_min: Mapped[Optional[float]] = mapped_column(Float)
    elevation_max: Mapped[Optional[float]] = mapped_column(Float)
    density_pts_per_m2: Mapped[Optional[float]] = mapped_column(Float)
    has_rgb: Mapped[bool] = mapped_column(Boolean, default=False)
    has_intensity: Mapped[bool] = mapped_column(Boolean, default=True)
    las_version: Mapped[Optional[str]] = mapped_column(String(10))

    # Potree output
    potree_path: Mapped[Optional[str]] = mapped_column(String(500))  # relative path
    hierarchy_step_size: Mapped[int] = mapped_column(Integer, default=1000)
    lod_levels: Mapped[Optional[int]] = mapped_column(Integer)

    # Processing metadata
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    processing_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    processing_duration_s: Mapped[Optional[float]] = mapped_column(Float)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    extra_meta: Mapped[Optional[dict]] = mapped_column(JSONB)

    # Relationships
    owner: Mapped[User] = relationship("User", back_populates="datasets")
    jobs: Mapped[list[ProcessingJob]] = relationship("ProcessingJob", back_populates="dataset")
    upload_session: Mapped[Optional[UploadSession]] = relationship(
        "UploadSession", back_populates="dataset", uselist=False
    )


class UploadSession(TimestampMixin, Base):
    """Tracks chunked upload sessions for large files."""

    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    dataset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_bitmask: Mapped[Optional[str]] = mapped_column(Text)  # JSON array of received chunk indices
    temp_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_crs_override: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="upload_sessions")
    dataset: Mapped[Optional[Dataset]] = relationship("Dataset", back_populates="upload_session")


class ProcessingJob(TimestampMixin, Base):
    """Celery job tracking with retry support."""

    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False, index=True
    )
    celery_task_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.QUEUED, nullable=False, index=True
    )
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "full_pipeline", "reprojection", etc.

    # Progress
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_step: Mapped[Optional[str]] = mapped_column(String(100))
    steps_completed: Mapped[Optional[dict]] = mapped_column(JSONB)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Retry
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    error_traceback: Mapped[Optional[str]] = mapped_column(Text)

    # Relationships
    dataset: Mapped[Dataset] = relationship("Dataset", back_populates="jobs")
