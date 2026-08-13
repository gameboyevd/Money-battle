​import asyncio
from functools import wraps
import discord
from discord import app_commands
from utils.db import Database

# 사용자별 동시 실행 방지를 위한 메모리 락 집합 (User ID 저장)
_active_user_locks = set()
_lock_mutex = asyncio.Lock()


async def acquire_user_lock(user_id: int) -> bool:
    """
    유저 락 획득 시도
    이미 작업 진행 중이면 False, 락 획득 성공 시 True 반환
    """
    async with _lock_mutex:
        if user_id in _active_user_locks:
            return False
        _active_user_locks.add(user_id)
        return True


async def release_user_lock(user_id: int) -> None:
    """유저 락 해제"""
    async with _lock_mutex:
        _active_user_locks.discard(user_id)


def in_action_lock():
    """
    [데코레이터] 슬래시 명령어 동시 실행 방지
    유저가 게임/상점 등 처리 중일 때 중복 입력을 막아줍니다.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
            user_id = interaction.user.id

            # 락 획득 시도
            if not await acquire_user_lock(user_id):
                await interaction.response.send_message(
                    "⚠️ 이전 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요!",
                    ephemeral=True
                )
                return

            try:
                # 본래 명령어 로직 실행
                return await func(self, interaction, *args, **kwargs)
            finally:
                # 작업 완료 후(에러 발생 포함) 반드시 락 해제
                await release_user_lock(user_id)

        return wrapper
    return decorator


async def check_player_registered(user_id: int) -> bool:
    """플레이어가 DB에 등록되어 있는지 확인"""
    pool = Database.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM players WHERE discord_id = $1", 
            str(user_id)
        )
        return row is not None


async def check_player_active(user_id: int) -> tuple[bool, str]:
    """
    플레이어 상태 및 파산 여부 확인
    반환: (진행가능 여부, 사유/상태)
    """
    pool = Database.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT money, status FROM players WHERE discord_id = $1", 
            str(user_id)
        )
        if not row:
            return False, "UNREGISTERED"
        
        if row["status"] == "BANKRUPT":
            return False, "BANKRUPT"
            
        return True, row["status"]
