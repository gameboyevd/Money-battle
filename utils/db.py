import asyncpg
from config import DATABASE_URL

class Database:
    pool: asyncpg.Pool = None

    @classmethod
    async def init_pool(cls):
        if not cls.pool:
            cls.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=2,
                max_size=10
            )

    @classmethod
    async def close_pool(cls):
        if cls.pool:
            await cls.pool.close()

    @classmethod
    def get_pool(cls) -> asyncpg.Pool:
        return cls.pool
