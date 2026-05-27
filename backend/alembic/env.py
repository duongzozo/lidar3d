"""Alembic environment configuration for async SQLAlchemy."""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings
from app.models.models import Base

# this is the Alembic Config object
config = context.config

# Override sqlalchemy.url from app settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("+asyncpg", "+asyncpg"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    def include_object(object, name, type_, reflected, compare_to):
        # Exclude PostGIS extension tables and tiger geocoder schemas
        if type_ == "table" and reflected:
            schema = getattr(object, "schema", None)
            if schema in ("tiger", "topology"):
                return False
            # Exclude known PostGIS internal tables
            postgis_tables = {
                "spatial_ref_sys", "geometry_columns", "geography_columns",
                "raster_columns", "raster_overviews",
                "loader_lookuptables", "loader_variables", "loader_platform",
                "geocode_settings", "geocode_settings_default",
                "pagc_gaz", "pagc_lex", "pagc_rules",
                "county", "county_lookup", "countysub_lookup",
                "cousub", "edges", "faces", "featnames",
                "place", "place_lookup", "state", "state_lookup",
                "tabblock", "tabblock20", "tract", "zcta5", "zip_lookup",
                "zip_lookup_all", "zip_lookup_base", "zip_state",
                "zip_state_loc", "addrfeat", "addr",
            }
            if name in postgis_tables:
                return False
        return True

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
