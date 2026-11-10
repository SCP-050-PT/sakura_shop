import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base

config = context.config
# Only the CLI has an ini file; when the bot runs migrations it keeps its own logging setup.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class DbSettings(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")
        database_url: str = "sqlite+aiosqlite:///data/shop.db"

    return DbSettings().database_url


def configure(connection) -> None:  # type: ignore[no-untyped-def]
    # Batch mode lets ALTER-style changes work on SQLite, which cannot alter columns in place.
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
