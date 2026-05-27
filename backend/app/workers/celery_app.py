"""Celery application and async processing tasks."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
from celery import Celery
from celery.signals import worker_ready
from sqlalchemy import select, update

from app.core.config import settings

log = structlog.get_logger()

# ── Celery App ────────────────────────────────────────────────────────────

celery_app = Celery(
    "lidar3d",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    result_expires=86400,  # 24h
    task_soft_time_limit=6600,
    task_time_limit=7200,
    task_routes={
        "app.workers.tasks.process_lidar_file": {"queue": "lidar_processing"},
        "app.workers.tasks.cleanup_expired_sessions": {"queue": "default"},
    },
    beat_schedule={
        "cleanup-expired-sessions": {
            "task": "app.workers.tasks.cleanup_expired_sessions",
            "schedule": 3600.0,  # every hour
        },
    },
)
