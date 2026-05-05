from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from taghdev.settings import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    # Never use echo=True — SQLAlchemy adds a StreamHandler(sys.stdout) which
    # corrupts the JSON-RPC stream when this engine is used inside MCP subprocess servers.
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


async def dispose_engine():
    """Dispose engine connection pool. Call on shutdown."""
    await engine.dispose()
