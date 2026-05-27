"""
Chunked file upload API.

Flow:
  1. POST /upload/init      → create UploadSession, get session_id
  2. PUT  /upload/{id}/chunk?index=N  (repeat for all chunks)
  3. POST /upload/{id}/complete       → merge chunks, start processing
  4. GET  /upload/{id}/status         → poll upload progress
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import aiofiles
import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import CurrentUser
from app.db.session import DBSession
from app.models.models import Dataset, DatasetStatus, ProcessingJob, UploadSession
from app.workers.tasks import process_lidar_file

log = structlog.get_logger()
router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_EXTENSIONS = {".las", ".laz"}


# ── Pydantic schemas ──────────────────────────────────────────────────────

class UploadInitRequest(BaseModel):
    filename: str
    total_size_bytes: int
    chunk_size_bytes: int
    dataset_name: str
    dataset_description: str = ""
    source_crs: str | None = None  # e.g. "EPSG:3405" for VN-2000 TM-3 105°


class UploadInitResponse(BaseModel):
    session_id: str
    total_chunks: int
    chunk_size_bytes: int
    expires_at: str


class UploadStatusResponse(BaseModel):
    session_id: str
    filename: str
    uploaded_chunks: int
    total_chunks: int
    progress_pct: float
    is_complete: bool
    dataset_id: str | None


# ── Helpers ───────────────────────────────────────────────────────────────

def _validate_filename(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {suffix!r} not allowed. Only LAS/LAZ files accepted.",
        )


def _get_upload_temp_dir(session_id: uuid.UUID) -> Path:
    d = settings.UPLOAD_DIR / "sessions" / str(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _merge_chunks(session: UploadSession, merged_path: Path) -> str:
    """Merge all chunks into a single file, return SHA-256 hash."""
    tmp = _get_upload_temp_dir(session.id)
    h = hashlib.sha256()

    async with aiofiles.open(merged_path, "wb") as out:
        for idx in range(session.total_chunks):
            chunk_file = tmp / f"chunk_{idx:05d}"
            if not chunk_file.exists():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Missing chunk {idx}",
                )
            async with aiofiles.open(chunk_file, "rb") as chunk:
                while True:
                    data = await chunk.read(8 * 1024 * 1024)  # 8MB read buffer
                    if not data:
                        break
                    h.update(data)
                    await out.write(data)

    return h.hexdigest()


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/init", response_model=UploadInitResponse, status_code=status.HTTP_201_CREATED)
async def init_upload(
    body: UploadInitRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> UploadInitResponse:
    """Initialize a new chunked upload session."""
    _validate_filename(body.filename)

    max_bytes = settings.MAX_UPLOAD_SIZE_BYTES
    if body.total_size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {settings.MAX_UPLOAD_SIZE_GB} GB",
        )

    effective_chunk = min(body.chunk_size_bytes, settings.CHUNK_SIZE_BYTES)
    total_chunks = math.ceil(body.total_size_bytes / effective_chunk)
    session_id = uuid.uuid4()
    temp_dir = _get_upload_temp_dir(session_id)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    session = UploadSession(
        id=session_id,
        user_id=current_user.id,
        filename=body.filename,
        total_size_bytes=body.total_size_bytes,
        chunk_size_bytes=effective_chunk,
        total_chunks=total_chunks,
        uploaded_chunks=0,
        temp_dir=str(temp_dir),
        expires_at=expires_at,
        is_complete=False,
        source_crs_override=body.source_crs,
    )
    db.add(session)
    await db.flush()

    # Create placeholder dataset record
    dataset = Dataset(
        owner_id=current_user.id,
        name=body.dataset_name,
        description=body.dataset_description,
        original_filename=body.filename,
        status=DatasetStatus.UPLOADING,
    )
    db.add(dataset)
    await db.flush()
    session.dataset_id = dataset.id
    await db.commit()

    log.info("upload_session_created", session_id=str(session_id), chunks=total_chunks)

    return UploadInitResponse(
        session_id=str(session_id),
        total_chunks=total_chunks,
        chunk_size_bytes=effective_chunk,
        expires_at=expires_at.isoformat(),
    )


@router.put("/{session_id}/chunk")
async def upload_chunk(
    session_id: uuid.UUID,
    current_user: CurrentUser,
    db: DBSession,
    chunk_index: int = Query(..., ge=0, alias="index"),
    file: UploadFile = File(...),
) -> JSONResponse:
    """Upload a single chunk of a file."""
    # Validate session (read-only, no lock needed for this)
    result = await db.execute(
        select(UploadSession).where(
            UploadSession.id == session_id,
            UploadSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if session.is_complete:
        raise HTTPException(status_code=400, detail="Upload session already completed")
    if datetime.now(timezone.utc) > session.expires_at:
        raise HTTPException(status_code=410, detail="Upload session expired")
    if chunk_index >= session.total_chunks:
        raise HTTPException(status_code=400, detail=f"Chunk index {chunk_index} out of range")

    # Write chunk to disk
    tmp = Path(session.temp_dir)
    chunk_path = tmp / f"chunk_{chunk_index:05d}"
    async with aiofiles.open(chunk_path, "wb") as f:
        while True:
            data = await file.read(1024 * 1024)
            if not data:
                break
            await f.write(data)

    # Atomic PostgreSQL UPDATE — append chunk_index to JSON array only if not present.
    # Single statement = no race condition, no read-modify-write.
    await db.execute(
        text("""
            UPDATE upload_sessions
            SET
                chunk_bitmask = CASE
                    WHEN COALESCE(chunk_bitmask, '[]')::jsonb @> to_jsonb(CAST(:idx AS integer))
                    THEN COALESCE(chunk_bitmask, '[]')
                    ELSE (COALESCE(chunk_bitmask, '[]')::jsonb || to_jsonb(CAST(:idx AS integer)))::text
                END,
                uploaded_chunks = (
                    SELECT jsonb_array_length(
                        CASE
                            WHEN COALESCE(chunk_bitmask, '[]')::jsonb @> to_jsonb(CAST(:idx AS integer))
                            THEN COALESCE(chunk_bitmask, '[]')::jsonb
                            ELSE COALESCE(chunk_bitmask, '[]')::jsonb || to_jsonb(CAST(:idx AS integer))
                        END
                    )
                )
            WHERE id = :sid
        """),
        {"idx": chunk_index, "sid": str(session_id)},
    )
    await db.commit()

    # Read updated count for response
    updated = await db.execute(
        select(UploadSession.uploaded_chunks, UploadSession.total_chunks)
        .where(UploadSession.id == session_id)
    )
    row = updated.one()

    return JSONResponse(
        content={
            "chunk_index": chunk_index,
            "uploaded_chunks": row.uploaded_chunks,
            "total_chunks": row.total_chunks,
        }
    )


@router.post("/{session_id}/complete")
async def complete_upload(
    session_id: uuid.UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> JSONResponse:
    """Merge chunks and enqueue processing job."""
    result = await db.execute(
        select(UploadSession).where(
            UploadSession.id == session_id,
            UploadSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.is_complete:
        raise HTTPException(status_code=400, detail="Already completed")

    received: list[int] = json.loads(session.chunk_bitmask or "[]")
    if len(received) < session.total_chunks:
        missing = [i for i in range(session.total_chunks) if i not in received]
        raise HTTPException(
            status_code=400,
            detail=f"Missing chunks: {missing[:20]}{'...' if len(missing) > 20 else ''}",
        )

    # Merge chunks
    dataset_out_dir = settings.OUTPUT_DIR / str(session.dataset_id)
    dataset_out_dir.mkdir(parents=True, exist_ok=True)
    merged_path = dataset_out_dir / session.filename

    file_hash = await _merge_chunks(session, merged_path)

    # Cleanup temp chunks
    shutil.rmtree(session.temp_dir, ignore_errors=True)

    # Update session + dataset
    await db.execute(
        update(UploadSession)
        .where(UploadSession.id == session_id)
        .values(is_complete=True)
    )
    await db.execute(
        update(Dataset)
        .where(Dataset.id == session.dataset_id)
        .values(
            file_size_bytes=session.total_size_bytes,
            file_hash=file_hash,
            file_path=str(merged_path),
            status=DatasetStatus.PENDING,
        )
    )

    # Create ProcessingJob
    job = ProcessingJob(
        dataset_id=session.dataset_id,
        job_type="full_pipeline",
    )
    db.add(job)
    await db.flush()

    # Enqueue Celery task
    task = process_lidar_file.apply_async(
        kwargs={
            "dataset_id": str(session.dataset_id),
            "job_id": str(job.id),
            "input_path": str(merged_path),
            "output_dir": str(dataset_out_dir),
            "source_crs_override": session.source_crs_override,
        },
        queue="lidar_processing",
    )

    await db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job.id)
        .values(celery_task_id=task.id)
    )
    await db.commit()

    log.info("upload_complete_job_queued", dataset_id=str(session.dataset_id), task_id=task.id)

    return JSONResponse(
        content={
            "dataset_id": str(session.dataset_id),
            "job_id": str(job.id),
            "celery_task_id": task.id,
            "message": "Processing started",
        }
    )


@router.get("/{session_id}/status", response_model=UploadStatusResponse)
async def get_upload_status(
    session_id: uuid.UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> UploadStatusResponse:
    result = await db.execute(
        select(UploadSession).where(
            UploadSession.id == session_id,
            UploadSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    pct = (session.uploaded_chunks / max(session.total_chunks, 1)) * 100

    return UploadStatusResponse(
        session_id=str(session.id),
        filename=session.filename,
        uploaded_chunks=session.uploaded_chunks,
        total_chunks=session.total_chunks,
        progress_pct=round(pct, 1),
        is_complete=session.is_complete,
        dataset_id=str(session.dataset_id) if session.dataset_id else None,
    )
