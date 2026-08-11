import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands
import asyncpg


# ==========================================
# Render HTTP 서버
# ==========================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

        self.wfile.write(
            b"Money Battle Royale Bot is running!"
        )

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(f"HTTP server started on port {port}")

    server.serve_forever()


# ==========================================
# Discord Bot
# ==========================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ==========================================
# DB 테스트
# ==========================================

async def test_database():

    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        print("DATABASE_URL is missing.")
        return

    try:
        connection = await asyncpg.connect(
            database_url
        )

        result = await connection.fetchval(
            "SELECT 1;"
        )

        await connection.close()

        if result == 1:
            print("Database connected successfully!")

    except Exception as e:
        print("Database connection failed:")
        print(type(e).__name__)
        print(str(e))


# ==========================================
# 플레이어 등록
# ==========================================

async def get_or_create_player(user: discord.User):

    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL이 설정되지 않았습니다."
        )

    connection = await asyncpg.connect(
        database_url
    )

    try:

        player = await connection.fetchrow(
            """
            INSERT INTO players (
                discord_id,
                username
            )
            VALUES ($1, $2)

            ON CONFLICT (discord_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                updated_at = NOW()

            RETURNING *
            """,
            str(user.id),
            user.name
        )

        return player

    finally:

        await connection.close()


# ==========================================
# 메인 메뉴
# ==========================================

class MainView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=180
        )


    # --------------------------------------
    # 내 정보
    # --------------------------------------

    @discord.ui.button(
        label="내 정보",
        emoji="👤",
        style=discord.ButtonStyle.primary
    )
    async def my_info(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            player = await get_or_create_player(
                interaction.user
            )

            await interaction.response.send_message(

                f"👤 **{interaction.user.display_name}님의 정보**\n\n"
                f"💰 게임머니: **{player['money']:,}**\n"
                f"💎 다이아: **{player['diamonds']:,}**\n"
                f"⭐ 포인트: **{player['points']:,}**\n"
                f"😇 선행 포인트: **{player['good_deed']:,}**",

                ephemeral=True
            )

        except Exception as e:

            print("My info error:")
            print(type(e).__name__)
            print(str(e))

            await interaction.response.send_message(
                "🔴 정보를 불러오는 중 오류가 발생했습니다.",
                ephemeral=True
            )


# ==========================================
# /메인
# ==========================================

@bot.tree.command(
    name="메인",
    description="머니 배틀로얄 메인 메뉴를 엽니다."
)
async def main_menu(
    interaction: discord.Interaction
):

    try:

        await get_or_create_player(
            interaction.user
        )

except Exception as e:

    print("Player registration error:")
    print(type(e).__name__)
    print(str(e))

    await interaction.response.send_message(
        f"🔴 플레이어 생성 실패\n"
        f"오류: `{type(e).__name__}`\n"
        f"내용: `{str(e)[:500]}`",
        ephemeral=True
    )

    return


    embed = discord.Embed(
        title="💰 머니 배틀로얄",
        description=(
            "돈을 벌고, 아이템을 사고,\n"
            "마지막까지 살아남으세요!\n\n"
            "아래 버튼에서 내 정보를 확인할 수 있습니다."
        )
    )


    await interaction.response.send_message(
        embed=embed,
        view=MainView(),
        ephemeral=True
    )


# ==========================================
# /dbtest
# ==========================================

@bot.tree.command(
    name="dbtest",
    description="Supabase 데이터베이스 연결을 테스트합니다."
)
async def dbtest(
    interaction: discord.Interaction
):

    database_url = os.environ.get(
        "DATABASE_URL"
    )

    if not database_url:

        await interaction.response.send_message(
            "🔴 DATABASE_URL이 설정되어 있지 않습니다.",
            ephemeral=True
        )

        return


    try:

        connection = await asyncpg.connect(
            database_url
        )

        result = await connection.fetchval(
            "SELECT 1;"
        )

        await connection.close()


        if result == 1:

            await interaction.response.send_message(
                "🟢 Supabase 데이터베이스 연결 성공!",
                ephemeral=True
            )


    except Exception as e:

        print("Database test failed:")
        print(type(e).__name__)
        print(str(e))

        await interaction.response.send_message(
            f"🔴 DB 연결 실패\n"
            f"오류: `{type(e).__name__}`\n"
            f"내용: `{str(e)[:500]}`",
            ephemeral=True
        )


# ==========================================
# Discord 이벤트
# ==========================================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user}"
    )

    print(
        "Money Battle Royale Bot is ready!"
    )


    try:

        synced = await bot.tree.sync()

        print(
            f"Slash commands synced: {len(synced)}"
        )

    except Exception as e:

        print(
            "Slash command sync failed:"
        )

        print(
            type(e).__name__
        )

        print(
            str(e)
        )


    await test_database()


# ==========================================
# 실행
# ==========================================

if __name__ == "__main__":

    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()


    token = os.environ.get(
        "DISCORD_TOKEN"
    )

    if not token:

        raise RuntimeError(
            "DISCORD_TOKEN environment variable is missing."
        )


    bot.run(token)
