from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from database.models import Base

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout_seconds,
    pool_recycle=settings.db_pool_recycle_seconds,
)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# bot + web оба зовут init_db при старте; без лока ENUM create_all падает гонкой
_SCHEMA_LOCK_ID = 872314205


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK_ID})"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ")
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reengage_level INTEGER DEFAULT 0"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_test BOOLEAN DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS language_chosen BOOLEAN DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_suspicious BOOLEAN DEFAULT FALSE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspicious_reason TEXT"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspicious_at TIMESTAMPTZ"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_likes_from_created "
                "ON likes (from_user_id, created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_likes_to_action_seen "
                "ON likes (to_user_id, action, is_seen)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_likes_to_created "
                "ON likes (to_user_id, created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_reports_to_created "
                "ON reports (to_user_id, created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_profiles_feed "
                "ON profiles (gender, looking_for, lat, lon, age) "
                "WHERE is_active = TRUE AND is_complete = TRUE "
                "AND lat IS NOT NULL AND lon IS NOT NULL"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_users_reengage_activity "
                "ON users (last_activity_at) "
                "WHERE last_activity_at IS NOT NULL AND reengage_level < 3 "
                "AND is_test = FALSE AND is_blocked = FALSE"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_settlement_aliases_name_norm "
                "ON settlement_aliases (name_norm)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE settlements ADD COLUMN IF NOT EXISTS population INTEGER DEFAULT 0"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE likes ADD COLUMN IF NOT EXISTS message_payload JSONB"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS photo_file_ids JSONB"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE premium_orders ADD COLUMN IF NOT EXISTS receipt_file_id VARCHAR(256)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE premium_orders ADD COLUMN IF NOT EXISTS receipt_kind VARCHAR(16)"
            )
        )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
