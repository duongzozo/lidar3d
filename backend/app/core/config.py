"""Application configuration using Pydantic Settings."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────
    APP_NAME: str = "LiDAR3D"
    APP_ENV: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "dev-secret-change-in-production"

    # ── Database ─────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://lidar3d:lidar3d@localhost:5432/lidar3d"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 40
    DATABASE_POOL_TIMEOUT: int = 30

    # ── Redis ────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── JWT ──────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "dev-jwt-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Storage ──────────────────────────────────────────────────
    UPLOAD_DIR: Path = Path("/data/uploads")
    OUTPUT_DIR: Path = Path("/data/output")
    MAX_UPLOAD_SIZE_GB: float = 10.0
    CHUNK_SIZE_MB: int = 64

    # ── CORS ─────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost",
        "http://localhost:80",
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [origin.strip() for origin in v.split(",")]
        return v

    # ── Processing ───────────────────────────────────────────────
    PDAL_THREADS: int = 4
    POTREE_CONVERTER_PATH: str = "/usr/local/bin/PotreeConverter"

    # ── Computed ─────────────────────────────────────────────────
    @property
    def MAX_UPLOAD_SIZE_BYTES(self) -> int:
        return int(self.MAX_UPLOAD_SIZE_GB * 1024**3)

    @property
    def CHUNK_SIZE_BYTES(self) -> int:
        return self.CHUNK_SIZE_MB * 1024 * 1024

    def model_post_init(self, __context: Any) -> None:
        # Best-effort mkdir — will succeed inside container where /data is mounted
        try:
            self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            self.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # lifespan() will retry after volumes are mounted


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
