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
# DB 연결
# ==========================================

async def get_db():

    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL이 설정되지 않았습니다."
        )

    return await asyncpg.connect(database_url)


# ==========================================
# DB 테스트
# ==========================================

async def test_database():

    try:

        connection = await get_db()

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

async def get_or_create_player(
    user: discord.User,
    guild: discord.Guild
):

    if guild is None:
        raise RuntimeError(
            "디스코드 서버에서만 사용할 수 있습니다."
        )

    connection = await get_db()

    try:

        player = await connection.fetchrow(
            """
            INSERT INTO players (
                server_id,
                user_id,
                discord_id,
                username
            )
            VALUES ($1, $2, $2, $3)

            ON CONFLICT (server_id, discord_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                updated_at = NOW()

            RETURNING *
            """,
            str(guild.id),
            str(user.id),
            user.name
        )

        return player

    finally:

        await connection.close()


# ==========================================
# 현재 대기 게임 찾기 / 생성
# ==========================================

async def get_or_create_waiting_game(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel
):

    connection = await get_db()

    try:

        # 현재 채널에 대기 중인 게임이 있는지 확인
        game = await connection.fetchrow(
            """
            SELECT *
            FROM games
            WHERE channel_id = $1
              AND status = 'waiting'
            ORDER BY id DESC
            LIMIT 1
            """,
            str(channel.id)
        )

        if game:
            return game

        # 없으면 새 게임 생성
        game = await connection.fetchrow(
            """
            INSERT INTO games (
                game_type,
                status,
                host_id,
                channel_id,
                current_phase
            )
            VALUES (
                'money_battle_royale',
                'waiting',
                $1,
                $2,
                'waiting'
            )
            RETURNING *
            """,
            str(guild.owner_id),
            str(channel.id)
        )

        return game

    finally:

        await connection.close()


# ==========================================
# 게임 참가
# ==========================================

async def join_game_player(
    user: discord.User,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel
):

    if guild is None:
        raise RuntimeError(
            "디스코드 서버에서만 사용할 수 있습니다."
        )

    # 플레이어 먼저 등록
    await get_or_create_player(
        user,
        guild
    )

    # 대기 게임 가져오기
    game = await get_or_create_waiting_game(
        guild,
        channel
    )

    connection = await get_db()

    try:

        # 이미 참가했는지 확인
        existing = await connection.fetchrow(
            """
            SELECT *
            FROM game_players
            WHERE game_id = $1
              AND user_id = $2
            """,
            game["id"],
            str(user.id)
        )

        if existing:

            count = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM game_players
                WHERE game_id = $1
                """,
                game["id"]
            )

            return game, False, count

        # 참가 등록
        await connection.execute(
            """
            INSERT INTO game_players (
                game_id,
                user_id
            )
            VALUES ($1, $2)
            """,
            game["id"],
            str(user.id)
        )

        # 현재 참가자 수
        count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players
            WHERE game_id = $1
            """,
            game["id"]
        )

        return game, True, count

    finally:

        await connection.close()


# ==========================================
# 게임 참가 취소
# ==========================================

async def cancel_game_player(
    user: discord.User,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel
):

    if guild is None:
        raise RuntimeError(
            "디스코드 서버에서만 사용할 수 있습니다."
        )

    connection = await get_db()

    try:

        # 현재 채널의 대기 게임 찾기
        game = await connection.fetchrow(
            """
            SELECT *
            FROM games
            WHERE channel_id = $1
              AND status = 'waiting'
            ORDER BY id DESC
            LIMIT 1
            """,
            str(channel.id)
        )

        if not game:
            return None, False, 0

        # 참가 여부 확인
        existing = await connection.fetchrow(
            """
            SELECT *
            FROM game_players
            WHERE game_id = $1
              AND user_id = $2
            """,
            game["id"],
            str(user.id)
        )

        if not existing:

            count = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM game_players
                WHERE game_id = $1
                """,
                game["id"]
            )

            return game, False, count

        # 참가 취소
        await connection.execute(
            """
            DELETE FROM game_players
            WHERE game_id = $1
              AND user_id = $2
            """,
            game["id"],
            str(user.id)
        )

        # 남은 참가자 수
        count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players
            WHERE game_id = $1
            """,
            game["id"]
        )

        return game, True, count

    finally:

        await connection.close()


# ==========================================
# 메인 메뉴
# ==========================================

class MainView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=300
        )


    # ======================================
    # 내 정보
    # ======================================

    @discord.ui.button(
        label="내 정보",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def my_info(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            player = await get_or_create_player(
                interaction.user,
                interaction.guild
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


    # ======================================
    # 게임 설명
    # ======================================

    @discord.ui.button(
        label="게임 설명",
        emoji="📖",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def game_info(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        embed = discord.Embed(
            title="📖 머니 배틀로얄",
            description=(
                "💰 돈을 벌고\n"
                "🎒 아이템을 사용하고\n"
                "🎮 여러 게임에서 경쟁하며\n"
                "🏆 마지막까지 살아남는 게임입니다."
            )
        )

        embed.add_field(
            name="👥 최소 인원",
            value="3명",
            inline=True
        )

        embed.add_field(
            name="💀 탈락",
            value="게임 규칙에 따라 진행",
            inline=True
        )

        embed.add_field(
            name="🏆 목표",
            value="최후의 1인이 되는 것",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


    # ======================================
    # 게임 참가
    # ======================================

    @discord.ui.button(
        label="게임 참가",
        emoji="🎮",
        style=discord.ButtonStyle.success,
        row=1
    )
    async def join_game(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            game, joined, count = await join_game_player(
                interaction.user,
                interaction.guild,
                interaction.channel
            )

            if not joined:

                await interaction.response.send_message(
                    f"⚠️ 이미 게임에 참가되어 있습니다!\n\n"
                    f"👥 현재 참가자: **{count}명**",
                    ephemeral=True
                )

                return

            if count < 3:

                await interaction.response.send_message(
                    f"🎮 게임 참가 완료!\n\n"
                    f"👥 현재 참가자: **{count}명**\n"
                    f"⚠️ 인원이 부족해 게임을 시작할 수 없어요!\n"
                    f"최소 **3명**이 필요합니다.",
                    ephemeral=True
                )

            else:

                await interaction.response.send_message(
                    f"🎉 게임 참가 완료!\n\n"
                    f"👥 현재 참가자: **{count}명**\n"
                    f"🟢 게임을 시작할 수 있습니다!",
                    ephemeral=True
                )

        except Exception as e:

            print("Join game error:")
            print(type(e).__name__)
            print(str(e))

            await interaction.response.send_message(
                f"🔴 게임 참가 중 오류가 발생했습니다.\n"
                f"오류: `{type(e).__name__}`\n"
                f"내용: `{str(e)[:300]}`",
                ephemeral=True
            )


    # ======================================
    # 참가 취소
    # ======================================

    @discord.ui.button(
        label="참가 취소",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        row=1
    )
    async def cancel_game(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            game, cancelled, count = await cancel_game_player(
                interaction.user,
                interaction.guild,
                interaction.channel
            )

            if game is None:

                await interaction.response.send_message(
                    "⚠️ 현재 참가할 수 있는 대기 게임이 없습니다.",
                    ephemeral=True
                )

                return

            if not cancelled:

                await interaction.response.send_message(
                    f"⚠️ 현재 게임에 참가하고 있지 않습니다.\n\n"
                    f"👥 현재 참가자: **{count}명**",
                    ephemeral=True
                )

                return

            if count < 3:

                await interaction.response.send_message(
                    f"❌ 게임 참가를 취소했습니다.\n\n"
                    f"👥 현재 참가자: **{count}명**\n"
                    f"⚠️ 인원이 부족해 게임을 시작할 수 없어요!",
                    ephemeral=True
                )

            else:

                await interaction.response.send_message(
                    f"❌ 게임 참가를 취소했습니다.\n\n"
                    f"👥 현재 참가자: **{count}명**",
                    ephemeral=True
                )

        except Exception as e:

            print("Cancel game error:")
            print(type(e).__name__)
            print(str(e))

            await interaction.response.send_message(
                f"🔴 참가 취소 중 오류가 발생했습니다.\n"
                f"오류: `{type(e).__name__}`\n"
                f"내용: `{str(e)[:300]}`",
                ephemeral=True
            )


    # ======================================
    # 닫기
    # ======================================

    @discord.ui.button(
        label="닫기",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        row=2
    )
    async def close_menu(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content="메인 메뉴를 닫았습니다.",
            embed=None,
            view=None
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
            interaction.user,
            interaction.guild
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
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 **MONEY BATTLE ROYALE**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "돈을 벌고, 아이템을 사용하고,\n"
            "마지막까지 살아남으세요!\n\n"
            "🎮 **게임 참가** — 게임에 참가합니다.\n"
            "❌ **참가 취소** — 참가를 취소합니다.\n"
            "📖 **게임 설명** — 기본 규칙을 확인합니다.\n"
            "👤 **내 정보** — 현재 자산을 확인합니다."
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
