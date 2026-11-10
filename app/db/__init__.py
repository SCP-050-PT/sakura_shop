import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base


def create_engine(url: str) -> AsyncEngine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url)
    if parsed.get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create tables straight from the models; used by tests. The bot uses migrate()."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
BASELINE_REVISION = "0001"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # configparser interpolation
    return cfg


async def migrate(engine: AsyncEngine, url: str) -> None:
    """Bring the schema to the latest revision, adopting databases created before migrations existed."""
    async with engine.connect() as conn:
        tables = set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))
    cfg = alembic_config(url)
    # alembic's env.py runs its own event loop, so it must not share ours.
    if "users" in tables and "alembic_version" not in tables:
        await asyncio.to_thread(command.stamp, cfg, BASELINE_REVISION)
    await asyncio.to_thread(command.upgrade, cfg, "head")
