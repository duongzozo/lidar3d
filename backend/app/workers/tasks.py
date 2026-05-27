"""Celery tasks for LiDAR file processing pipeline."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import redis
import structlog
from celery import Task
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.models import Dataset, DatasetStatus, JobStatus, ProcessingJob
from app.services.pdal_service import PDAlPipeline, PointCloudMetadata
from app.workers.celery_app import celery_app

log = structlog.get_logger()

# ── Sync DB for Celery (use sync driver) ─────────────────────────────────

SYNC_DB_URL = settings.DATABASE_URL.replace("+asyncpg", "+psycopg2")
sync_engine = create_engine(SYNC_DB_URL, pool_pre_ping=True)
SyncSession = sessionmaker(sync_engine, expire_on_commit=False)

# ── Redis for WebSocket pub/sub ───────────────────────────────────────────

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


def _publish_progress(dataset_id: str, pct: float, step: str, status: str = "processing") -> None:
    """Publish progress update via Redis pub/sub (consumed by WebSocket endpoint)."""
    import json
    channel = f"dataset:{dataset_id}:progress"
    payload = json.dumps({
        "dataset_id": dataset_id,
        "progress": pct,
        "step": step,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    redis_client.publish(channel, payload)
    # Also store last known state for new subscribers
    redis_client.setex(f"dataset:{dataset_id}:last_progress", 86400, payload)


def _update_job_progress(
    db: Session,
    job_id: uuid.UUID,
    pct: float,
    step: str,
    status: JobStatus = JobStatus.RUNNING,
) -> None:
    db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id)
        .values(
            progress_pct=pct,
            current_step=step,
            status=status,
            started_at=ProcessingJob.started_at,  # keep existing
        )
    )
    db.commit()


# ── Base Task class with retry logic ─────────────────────────────────────

class BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 60  # seconds

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        log.error("task_failed", task_id=task_id, exc=str(exc))

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        log.warning("task_retrying", task_id=task_id, exc=str(exc))


# ── Main Processing Task ──────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=BaseTask,
    name="app.workers.tasks.process_lidar_file",
    queue="lidar_processing",
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def process_lidar_file(
    self,
    dataset_id: str,
    job_id: str,
    input_path: str,
    output_dir: str,
    source_crs_override: str | None = None,
) -> dict:
    """Full LiDAR processing pipeline: reproject → filter → convert → metadata."""

    dataset_uuid = uuid.UUID(dataset_id)
    job_uuid = uuid.UUID(job_id)
    input_file = Path(input_path)
    out_dir = Path(output_dir)

    log.info("task_started", dataset_id=dataset_id, job_id=job_id, input=input_path)

    with SyncSession() as db:
        try:
            # Mark job as running
            db.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == job_uuid)
                .values(
                    status=JobStatus.RUNNING,
                    celery_task_id=self.request.id,
                    started_at=datetime.now(timezone.utc),
                )
            )
            db.execute(
                update(Dataset)
                .where(Dataset.id == dataset_uuid)
                .values(
                    status=DatasetStatus.PROCESSING,
                    processing_started_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

            # Progress callback bridges PDAL → WebSocket
            def on_progress(pct: float, step: str) -> None:
                _publish_progress(dataset_id, pct, step)
                _update_job_progress(db, job_uuid, pct, step)
                self.update_state(
                    state="PROGRESS",
                    meta={"progress": pct, "step": step},
                )

            # ── Run Pipeline ──────────────────────────────────────────
            pipeline = PDAlPipeline(
                input_path=input_file,
                output_dir=out_dir,
                potree_converter_path=settings.POTREE_CONVERTER_PATH,
                threads=settings.PDAL_THREADS,
                source_crs_override=source_crs_override,
            )
            pipeline.add_progress_callback(on_progress)

            # Run async pipeline in sync context
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                meta: PointCloudMetadata = loop.run_until_complete(pipeline.run())
            finally:
                loop.close()

            # ── Build PostGIS bbox polygon ────────────────────────────
            b = meta.bbox_3d
            bbox_wkt = (
                f"POLYGON(({b['xmin']} {b['ymin']}, "
                f"{b['xmax']} {b['ymin']}, "
                f"{b['xmax']} {b['ymax']}, "
                f"{b['xmin']} {b['ymax']}, "
                f"{b['xmin']} {b['ymin']}))"
            )

            # ── Save metadata ─────────────────────────────────────────
            potree_rel = str(out_dir.relative_to(settings.OUTPUT_DIR)) + "/potree"

            db.execute(
                update(Dataset)
                .where(Dataset.id == dataset_uuid)
                .values(
                    status=DatasetStatus.COMPLETED,
                    source_crs=meta.source_crs,
                    output_crs=meta.output_crs,
                    point_count=meta.point_count,
                    bbox_3d=meta.bbox_3d,
                    bbox_geom=f"SRID=4326;{bbox_wkt}",
                    center_lon=meta.center_lon,
                    center_lat=meta.center_lat,
                    center_elevation=meta.center_elevation,
                    elevation_min=meta.elevation_min,
                    elevation_max=meta.elevation_max,
                    density_pts_per_m2=meta.density_pts_per_m2,
                    has_rgb=meta.has_rgb,
                    has_intensity=meta.has_intensity,
                    las_version=meta.las_version,
                    lod_levels=meta.lod_levels,
                    potree_path=potree_rel,
                    processing_completed_at=datetime.now(timezone.utc),
                )
            )
            db.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == job_uuid)
                .values(
                    status=JobStatus.COMPLETED,
                    progress_pct=100.0,
                    current_step="Done",
                    completed_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

            _publish_progress(dataset_id, 100.0, "Done", status="completed")
            log.info("task_completed", dataset_id=dataset_id)

            return {"status": "completed", "dataset_id": dataset_id}

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("task_error", dataset_id=dataset_id, error=str(exc), traceback=tb)

            retry_count = self.request.retries
            if retry_count < self.max_retries:
                # Update retry count in DB
                db.execute(
                    update(ProcessingJob)
                    .where(ProcessingJob.id == job_uuid)
                    .values(
                        status=JobStatus.RETRYING,
                        retry_count=retry_count + 1,
                        error_message=str(exc)[:500],
                        error_traceback=tb[:5000],
                    )
                )
                db.commit()
                _publish_progress(dataset_id, -1, f"Retrying ({retry_count + 1}/3)", "retrying")
                raise self.retry(exc=exc, countdown=120 * (retry_count + 1))

            # Final failure
            db.execute(
                update(Dataset)
                .where(Dataset.id == dataset_uuid)
                .values(
                    status=DatasetStatus.FAILED,
                    error_message=str(exc)[:1000],
                )
            )
            db.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == job_uuid)
                .values(
                    status=JobStatus.FAILED,
                    error_message=str(exc)[:500],
                    error_traceback=tb[:5000],
                    completed_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            _publish_progress(dataset_id, -1, f"Failed: {str(exc)[:100]}", "failed")
            raise


# ── Cleanup Task ──────────────────────────────────────────────────────────

@celery_app.task(name="app.workers.tasks.cleanup_expired_sessions", queue="default")
def cleanup_expired_sessions() -> dict:
    """Remove expired upload sessions and their temp files."""
    from datetime import datetime, timezone
    import shutil
    from sqlalchemy import delete
    from app.models.models import UploadSession

    cleaned = 0
    with SyncSession() as db:
        now = datetime.now(timezone.utc)
        expired = db.execute(
            select(UploadSession).where(
                UploadSession.expires_at < now,
                UploadSession.is_complete == False,
            )
        ).scalars().all()

        for session in expired:
            temp_dir = Path(session.temp_dir)
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
                cleaned += 1
            db.delete(session)

        db.commit()

    log.info("cleanup_complete", cleaned=cleaned)
    return {"cleaned_sessions": cleaned}
