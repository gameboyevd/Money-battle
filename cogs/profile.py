import asyncio
import os
import discord
from discord.ext import commands
from config import DISCORD_TOKEN
from utils.db import Database

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ 로그인 성공: {bot.user.name} ({bot.user.id})")
    
    # DB 커넥션 풀 초기화
    await Database.init_pool()
    print("🐘 PostgreSQL 커넥션 풀이 연결되었습니다.")

    # Slash Commands 동기화
    try:
        synced = await bot.tree.sync()
        print(f"🔄 슬래시 명령어 {len(synced)}개 동기화 완료")
    except Exception as e:
        print(f"❌ 명령어 동기화 실패: {e}")

async def load_extensions():
    """cogs 폴더 안의 모든 .py 파일을 읽어서 로드합니다."""
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("__"):
            await bot.load_extension(f"cogs.{filename[:-3]}")
            print(f"📦 Cog 로드 완료: {filename[:-3]}")

async def main():
    async with bot:
        await load_extensions()
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
