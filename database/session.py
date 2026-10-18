"""Async database engine, session factory, SQLite WAL pragmas, and lifecycle management."""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from database.models import Base

logger = logging.getLogger(__name__)


def create_sqlite_async_engine(
    db_path: Optional[str] = None,
    echo: Optional[bool] = None,
    poolclass=None,
    **kwargs,
) -> AsyncEngine:
    """Create and configure an AsyncEngine with SQLite WAL pragmas and connection tuning.

    Args:
        db_path: Database URL or file path. If None, defaults to get_settings().DB_PATH.
        echo: If True, SQLAlchemy logs SQL statements. If None, defaults to get_settings().DEBUG.
        poolclass: Optional connection pool class (e.g. StaticPool for shared in-memory testing).
        **kwargs: Additional parameters passed to create_async_engine.

    Returns:
        Configured AsyncEngine instance.
    """
    if db_path is None:
        try:
            from config import get_settings
            db_path = get_settings().DB_PATH
        except Exception:
            db_path = "sqlite+aiosqlite:///cryptogram.db"

    if echo is None:
        try:
            from config import get_settings
            echo = get_settings().DEBUG
        except Exception:
            echo = False

    if "://" not in db_path:
        db_url = f"sqlite+aiosqlite:///{db_path}"
    else:
        db_url = db_path

    connect_args = kwargs.pop("connect_args", {})
    if "sqlite" in db_url:
        connect_args.setdefault("check_same_thread", False)

    engine_kwargs = {"echo": echo, "connect_args": connect_args, **kwargs}
    if poolclass is not None:
        engine_kwargs["poolclass"] = poolclass

    engine = create_async_engine(db_url, **engine_kwargs)

    if "sqlite" in db_url:
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            """Apply SQLite performance and concurrency pragmas upon connection checkout."""
            cursor = dbapi_connection.cursor()
            try:
                # 1. WAL mode: enables non-blocking concurrent readers while writers write
                cursor.execute("PRAGMA journal_mode=WAL;")
                # 2. Balanced disk sync: provides ACID durability with minimal I/O latency
                cursor.execute("PRAGMA synchronous=NORMAL;")
                # 3. Foreign key constraints: enabled explicitly (disabled by default in SQLite)
                cursor.execute("PRAGMA foreign_keys=ON;")
                # 4. Busy timeout: wait up to 5000ms before throwing sqlite3.OperationalError: locked
                cursor.execute("PRAGMA busy_timeout=5000;")
                # 5. Cache size: 64MB memory page cache
                cursor.execute("PRAGMA cache_size=-64000;")
            finally:
                cursor.close()

    return engine


# Ergonomic alias
create_app_engine = create_sqlite_async_engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async_sessionmaker bound to the provided engine.

    Crucial setting: expire_on_commit=False prevents MissingGreenlet exceptions
    when accessing loaded model attributes after committing a transaction.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# Ergonomic alias
create_sessionmaker = create_session_factory


def _migrate_sqlite_schema(sync_conn) -> None:
    """Safely apply idempotent column migrations for pre-existing SQLite databases."""
    if sync_conn.dialect.name != "sqlite":
        return

    result = sync_conn.exec_driver_sql("PRAGMA table_info(users);")
    rows = result.fetchall()
    if not rows:
        return

    existing_cols = {row[1] for row in rows}

    columns_to_add = [
        ("exp", "INTEGER NOT NULL DEFAULT 0"),
        ("games_as_analyst", "INTEGER NOT NULL DEFAULT 0"),
        ("games_as_agent", "INTEGER NOT NULL DEFAULT 0"),
        ("total_hints_used", "INTEGER NOT NULL DEFAULT 0"),
        ("total_mistakes", "INTEGER NOT NULL DEFAULT 0"),
    ]

    for col_name, col_def in columns_to_add:
        if col_name not in existing_cols:
            sync_conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")
            logger.info("Migrated users table: added column %s", col_name)

    # Migrate game_sessions table
    result_sessions = sync_conn.exec_driver_sql("PRAGMA table_info(game_sessions);")
    rows_sessions = result_sessions.fetchall()
    if rows_sessions:
        existing_session_cols = {row[1] for row in rows_sessions}
        if "is_solo" not in existing_session_cols:
            sync_conn.exec_driver_sql("ALTER TABLE game_sessions ADD COLUMN is_solo BOOLEAN NOT NULL DEFAULT 0;")
            logger.info("Migrated game_sessions table: added column is_solo")



async def init_db(engine: AsyncEngine) -> None:
    """Idempotently initialize all database tables from SQLAlchemy metadata."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite_schema)
    logger.info("Database tables initialized successfully.")


async def close_db(engine: AsyncEngine) -> None:
    """Dispose of the database engine and close all pooled connections."""
    await engine.dispose()
    logger.info("Database engine disposed.")


@asynccontextmanager
async def get_db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional AsyncSession scope with automatic commit on success and rollback on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
