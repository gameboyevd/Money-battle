import os
import asyncio
import discord
from discord.ext import commands
from server import start_web_server

# Discord Bot Intents 설정
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    
    # 슬래시 명령어 동기화 (Slash Commands Sync)
    try:
        synced = await bot.tree.sync()
        print(f"🔄 슬래시 명령어 {len(synced)}개 동기화 완료")
    except Exception as e:
        print(f"❌ 명령어 동기화 실패: {e}")

    print("---------------------------------------------")


async def load_extensions():
    """cogs 폴더 안의 모든 Cog 파일 자동 로드"""
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("__"):
            await bot.load_extension(f"cogs.{filename[:-3]}")
            print(f"Loaded Cog: {filename[:-3]}")


async def main():
    # Render 웹서버 스레드 구동
    start_web_server()

    async with bot:
        await load_extensions()
        
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")
            
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
