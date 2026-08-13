import os
import asyncio
import discord
from discord.ext import commands
from server import start_web_server

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    
    # 슬래시 명령어 동기화
    try:
        synced = await bot.tree.sync()
        print(f"🔄 슬래시 명령어 {len(synced)}개 동기화 완료")
    except Exception as e:
        print(f"❌ 명령어 동기화 실패: {e}")

    print("---------------------------------------------")


async def load_extensions():
    """cogs 폴더 및 그 하위 폴더의 모든 Cog 파일 재귀적 로드"""
    for root, _, files in os.walk("./cogs"):
        for filename in files:
            if filename.endswith(".py") and not filename.startswith("__"):
                # 파일 경로를 discord.ext.commands가 읽을 수 있는 모듈 형태(예: cogs.games.blackjack)로 변환
                rel_path = os.path.relpath(os.path.join(root, filename), ".")
                module_name = rel_path[:-3].replace(os.sep, ".")
                try:
                    await bot.load_extension(module_name)
                    print(f"✅ Loaded Cog: {module_name}")
                except Exception as e:
                    print(f"❌ Failed to load Cog {module_name}: {e}")


async def main():
    start_web_server()

    async with bot:
        await load_extensions()
        
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")
            
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
