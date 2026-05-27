"""FastAPI application factory and main entry point."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, datasets, upload, websocket
from app.core.config import settings
from app.db.session import engine
from app.models.models import Base

log = structlog.get_logger()


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", app=settings.APP_NAME, env=settings.APP_ENV)

    # Create DB tables (production should use Alembic migrations)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Ensure data directories
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    yield

    log.info("shutdown")
    await engine.dispose()


# ── App factory ───────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="LiDAR3D API",
        description="3D Web GIS system for LAS/LAZ point cloud processing",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # ── Middleware ──────────────────────────────────────────────────
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Process-Time"],
    )

    # ── Request timing ─────────────────────────────────────────────
    @app.middleware("http")
    async def add_process_time(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        response.headers["X-Process-Time"] = f"{duration:.4f}s"
        return response

    # ── Error handlers ─────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        import traceback
        log.error("unhandled_exception",
                  path=request.url.path,
                  error=str(exc),
                  traceback=traceback.format_exc())
        detail = str(exc) if settings.DEBUG else "Internal server error"
        return JSONResponse(status_code=500, content={"detail": detail})

    # ── Routers ────────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(datasets.router, prefix="/api/v1")
    app.include_router(upload.router, prefix="/api/v1")
    app.include_router(websocket.router)

    # ── Static files: Potree tiles ─────────────────────────────────
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/tiles", StaticFiles(directory=str(output_dir)), name="tiles")

    # ── Health check ───────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "app": settings.APP_NAME}

    @app.get("/api/v1/health", tags=["system"])
    async def api_health():
        return {"status": "ok", "version": "1.0.0"}

    return app


app = create_app()
