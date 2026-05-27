"""Initial schema: users, datasets, upload_sessions, processing_jobs

Revision ID: 0001_initial
Revises: 
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union
import sqlalchemy as sa
import geoalchemy2
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable PostGIS
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis_topology")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # Enums
    op.execute("CREATE TYPE userrole AS ENUM ('admin', 'operator', 'viewer')")
    op.execute("CREATE TYPE datasetstatus AS ENUM ('pending', 'uploading', 'uploaded', 'processing', 'completed', 'failed')")
    op.execute("CREATE TYPE jobstatus AS ENUM ('queued', 'running', 'completed', 'failed', 'retrying')")

    # users
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("hashed_password", sa.Text, nullable=False),
        sa.Column("role", sa.Enum("admin","operator","viewer", name="userrole"), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )

    # datasets
    op.create_table(
        "datasets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.Enum("pending","uploading","uploaded","processing","completed","failed", name="datasetstatus"), nullable=False, server_default="pending"),
        sa.Column("original_filename", sa.String(500), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("file_path", sa.Text, nullable=True),
        sa.Column("potree_path", sa.Text, nullable=True),
        sa.Column("point_count", sa.BigInteger, nullable=True),
        sa.Column("crs_original", sa.String(100), nullable=True),
        sa.Column("crs_epsg", sa.Integer, nullable=True),
        sa.Column("bbox_3d", sa.JSON, nullable=True),
        sa.Column("elevation_min", sa.Float, nullable=True),
        sa.Column("elevation_max", sa.Float, nullable=True),
        sa.Column("density_pts_per_m2", sa.Float, nullable=True),
        sa.Column("has_rgb", sa.Boolean, server_default="false"),
        sa.Column("has_intensity", sa.Boolean, server_default="false"),
        sa.Column("is_visible", sa.Boolean, server_default="true"),
        sa.Column("owner_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # PostGIS geometry column
    op.execute("""
        ALTER TABLE datasets
        ADD COLUMN bbox_geom geometry(Polygon, 4326)
    """)
    op.execute("CREATE INDEX idx_datasets_bbox_geom ON datasets USING GIST(bbox_geom)")
    op.create_index("idx_datasets_status", "datasets", ["status"])
    op.create_index("idx_datasets_owner", "datasets", ["owner_id"])

    # upload_sessions
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("dataset_id", sa.UUID(as_uuid=True), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("total_size", sa.BigInteger, nullable=False),
        sa.Column("chunk_size", sa.Integer, nullable=False),
        sa.Column("total_chunks", sa.Integer, nullable=False),
        sa.Column("uploaded_chunks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chunk_bitmask", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("temp_dir", sa.Text, nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # processing_jobs
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("dataset_id", sa.UUID(as_uuid=True), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("status", sa.Enum("queued","running","completed","failed","retrying", name="jobstatus"), nullable=False, server_default="queued"),
        sa.Column("progress_pct", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current_step", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_jobs_dataset", "processing_jobs", ["dataset_id"])
    op.create_index("idx_jobs_status", "processing_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_table("upload_sessions")
    op.drop_table("datasets")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS jobstatus")
    op.execute("DROP TYPE IF EXISTS datasetstatus")
    op.execute("DROP TYPE IF EXISTS userrole")
