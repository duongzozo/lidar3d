"""Dataset management REST API."""

from __future__ import annotations

import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from app.core.security import CurrentUser, RequireOperator
from app.db.session import DBSession
from app.models.models import Dataset, DatasetStatus, ProcessingJob

log = structlog.get_logger()
router = APIRouter(prefix="/datasets", tags=["datasets"])


# ── Schemas ───────────────────────────────────────────────────────────────

class DatasetSummary(BaseModel):
    id: str
    name: str
    description: Optional[str]
    status: str
    is_visible: bool
    point_count: Optional[int]
    source_crs: Optional[str]
    center_lon: Optional[float]
    center_lat: Optional[float]
    center_elevation: Optional[float]
    elevation_min: Optional[float]
    elevation_max: Optional[float]
    potree_url: Optional[str]
    has_rgb: bool
    has_intensity: bool
    lod_levels: Optional[int]
    file_size_bytes: Optional[int]
    original_filename: str
    created_at: str
    processing_progress: Optional[float] = None

    model_config = {"from_attributes": True}


class DatasetDetail(DatasetSummary):
    bbox_3d: Optional[dict]
    density_pts_per_m2: Optional[float]
    las_version: Optional[str]
    output_crs: str
    jobs: list[dict] = []


class PaginatedDatasets(BaseModel):
    items: list[DatasetSummary]
    total: int
    page: int
    page_size: int
    pages: int


class DatasetUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_visible: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────

def _potree_url(dataset: Dataset) -> Optional[str]:
    if dataset.potree_path:
        return f"/tiles/{dataset.potree_path}/metadata.json"
    return None


def _to_summary(ds: Dataset) -> DatasetSummary:
    return DatasetSummary(
        id=str(ds.id),
        name=ds.name,
        description=ds.description,
        status=ds.status.value,
        is_visible=ds.is_visible,
        point_count=ds.point_count,
        source_crs=ds.source_crs,
        center_lon=ds.center_lon,
        center_lat=ds.center_lat,
        center_elevation=ds.center_elevation,
        elevation_min=ds.elevation_min,
        elevation_max=ds.elevation_max,
        potree_url=_potree_url(ds),
        has_rgb=ds.has_rgb,
        has_intensity=ds.has_intensity,
        lod_levels=ds.lod_levels,
        file_size_bytes=ds.file_size_bytes,
        original_filename=ds.original_filename,
        created_at=ds.created_at.isoformat(),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("", response_model=PaginatedDatasets)
async def list_datasets(
    current_user: CurrentUser,
    db: DBSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    visible_only: bool = Query(False),
    search: Optional[str] = Query(None),
) -> PaginatedDatasets:
    """List all datasets with pagination and filtering."""
    q = select(Dataset).where(Dataset.owner_id == current_user.id)

    if status_filter:
        q = q.where(Dataset.status == DatasetStatus(status_filter))
    if visible_only:
        q = q.where(Dataset.is_visible == True)
    if search:
        q = q.where(Dataset.name.ilike(f"%{search}%"))

    # Count
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    # Paginate
    q = q.order_by(Dataset.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    datasets = (await db.execute(q)).scalars().all()

    return PaginatedDatasets(
        items=[_to_summary(ds) for ds in datasets],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, -(-total // page_size)),
    )


@router.get("/{dataset_id}", response_model=DatasetDetail)
async def get_dataset(
    dataset_id: uuid.UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> DatasetDetail:
    result = await db.execute(
        select(Dataset)
        .where(Dataset.id == dataset_id, Dataset.owner_id == current_user.id)
        .options(selectinload(Dataset.jobs))
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    jobs = [
        {
            "id": str(j.id),
            "status": j.status.value,
            "progress_pct": j.progress_pct,
            "current_step": j.current_step,
            "retry_count": j.retry_count,
            "error_message": j.error_message,
            "created_at": j.created_at.isoformat(),
        }
        for j in sorted(ds.jobs, key=lambda x: x.created_at, reverse=True)
    ]

    return DatasetDetail(
        **_to_summary(ds).model_dump(),
        bbox_3d=ds.bbox_3d,
        density_pts_per_m2=ds.density_pts_per_m2,
        las_version=ds.las_version,
        output_crs=ds.output_crs,
        jobs=jobs,
    )


@router.patch("/{dataset_id}", response_model=DatasetSummary)
async def update_dataset(
    dataset_id: uuid.UUID,
    body: DatasetUpdateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> DatasetSummary:
    result = await db.execute(
        select(Dataset).where(
            Dataset.id == dataset_id, Dataset.owner_id == current_user.id
        )
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    update_data = body.model_dump(exclude_none=True)
    if update_data:
        await db.execute(
            update(Dataset).where(Dataset.id == dataset_id).values(**update_data)
        )
        await db.commit()
        await db.refresh(ds)

    return _to_summary(ds)


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: uuid.UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> Response:
    import shutil
    from app.core.config import settings

    result = await db.execute(
        select(Dataset).where(
            Dataset.id == dataset_id, Dataset.owner_id == current_user.id
        )
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    dataset_dir = settings.OUTPUT_DIR / str(dataset_id)
    if dataset_dir.exists():
        shutil.rmtree(dataset_dir, ignore_errors=True)

    # Delete child records first (no CASCADE on FK)
    await db.execute(
        delete(ProcessingJob).where(ProcessingJob.dataset_id == dataset_id)
    )
    await db.delete(ds)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{dataset_id}/job-status")
async def get_job_status(
    dataset_id: uuid.UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> dict:
    """Get latest processing job status."""
    result = await db.execute(
        select(ProcessingJob)
        .where(ProcessingJob.dataset_id == dataset_id)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No job found")

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "progress_pct": job.progress_pct,
        "current_step": job.current_step,
        "retry_count": job.retry_count,
        "error_message": job.error_message,
        "celery_task_id": job.celery_task_id,
    }


# ── Reprocess endpoint ─────────────────────────────────────────────────────

class ReprocessRequest(BaseModel):
    source_crs: str  # e.g. "EPSG:3405"


@router.post("/{dataset_id}/reprocess", status_code=202)
async def reprocess_dataset(
    dataset_id: uuid.UUID,
    body: ReprocessRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> dict:
    """Re-trigger pipeline with an explicit CRS override (useful for VN-2000 etc.)."""
    import shutil
    from app.core.config import settings
    from app.models.models import JobStatus, ProcessingJob
    from app.workers.tasks import process_lidar_file

    result = await db.execute(
        select(Dataset).where(
            Dataset.id == dataset_id, Dataset.owner_id == current_user.id
        )
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not ds.file_path:
        raise HTTPException(status_code=400, detail="No source file on record")

    # Smart input file resolution:
    # 1. If original file exists, use it (full reprocess with CRS reprojection)
    # 2. If only reprojected.laz exists, use it (regenerate LOD tiles only)
    from pathlib import Path as _Path
    dataset_dir = settings.OUTPUT_DIR / str(dataset_id)
    candidates = [
        _Path(ds.file_path) if ds.file_path else None,
        dataset_dir / ds.original_filename if ds.original_filename else None,
        dataset_dir / "reprojected.laz",
    ]
    input_path = None
    for c in candidates:
        if c and c.exists():
            input_path = str(c)
            log.info("reprocess_input_resolved", path=input_path)
            break

    if not input_path:
        raise HTTPException(
            status_code=404,
            detail=f"Original file not found. Expected at: {ds.file_path or 'unknown'}. "
                   f"Please re-upload the dataset.",
        )

    output_dir = str(dataset_dir)

    # Only clean LOD output, KEEP source files
    potree_dir = dataset_dir / "potree"
    if potree_dir.exists():
        shutil.rmtree(potree_dir, ignore_errors=True)
    downsampled = dataset_dir / "downsampled.laz"
    downsampled.unlink(missing_ok=True)

    # Only delete reprojected.laz if we have the original (will regen)
    if _Path(input_path) != dataset_dir / "reprojected.laz":
        (dataset_dir / "reprojected.laz").unlink(missing_ok=True)

    # New processing job
    job = ProcessingJob(dataset_id=dataset_id, job_type="reprocess")
    db.add(job)
    await db.flush()

    await db.execute(
        update(Dataset).where(Dataset.id == dataset_id).values(
            status=DatasetStatus.PROCESSING
        )
    )
    await db.commit()

    task = process_lidar_file.apply_async(
        kwargs={
            "dataset_id": str(dataset_id),
            "job_id": str(job.id),
            "input_path": input_path,
            "output_dir": output_dir,
            "source_crs_override": body.source_crs,
        },
        queue="lidar_processing",
    )
    await db.execute(
        update(ProcessingJob).where(ProcessingJob.id == job.id)
        .values(celery_task_id=task.id)
    )
    await db.commit()

    return {"job_id": str(job.id), "task_id": task.id, "source_crs": body.source_crs}
