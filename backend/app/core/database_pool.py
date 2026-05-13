import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool
import logging
from ..config import settings

logger = logging.getLogger(__name__)

class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        
    async def initialize(self):
        """Initialize database connection pool"""
        if self.session_factory is not None:
            return

        try:
            # Use the configured DATABASE_URL (docker-compose injects the local
            # Postgres URL). The legacy code referenced `settings.supabase_db_*`
            # fields that do not exist on Settings, which silently dropped every
            # request to the mock fallback in `calculate_total_revenue` and
            # hid the real bugs.
            raw_url = settings.database_url or "postgresql://postgres:postgres@db:5432/propertyflow"
            if raw_url.startswith("postgresql+asyncpg://"):
                database_url = raw_url
            elif raw_url.startswith("postgresql://"):
                database_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            else:
                database_url = raw_url

            self.engine = create_async_engine(
                database_url,
                poolclass=AsyncAdaptedQueuePool,
                pool_size=20,
                max_overflow=30,
                pool_pre_ping=True,
                pool_recycle=3600,
                echo=False,
            )
            
            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False
            )
            
            logger.info("✅ Database connection pool initialized")
            
        except Exception as e:
            logger.error(f"❌ Database pool initialization failed: {e}")
            self.engine = None
            self.session_factory = None
    
    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
    
    def get_session(self) -> AsyncSession:
        """Get database session from pool.

        Returns an `AsyncSession` *instance* (not a coroutine) so callers can
        use it directly with `async with db_pool.get_session() as session:`.
        Marking this `async` would return a coroutine and break the context
        manager protocol — that's exactly what caused every revenue query to
        fall through to the mock fallback in `calculate_total_revenue`.
        """
        if not self.session_factory:
            raise Exception("Database pool not initialized")
        return self.session_factory()

# Global database pool instance
db_pool = DatabasePool()

async def get_db_session() -> AsyncSession:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
