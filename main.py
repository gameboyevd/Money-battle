import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands
import asyncpg


# ============================================================
# Render HTTP 서버
# ============================================================

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


# ============================================================
# Discord Bot
# ============================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# 기본 설정
# ============================================================

MIN_PLAYERS = 4
STARTING_MONEY = 10_000


# ============================================================
# DB
# ============================================================

async def get_db():

    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL이 설정되지 않았습니다."
        )

    return await asyncpg.connect(database_url)


# ============================================================
# 플레이어 등록
# ============================================================

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
                username,
                money,
                diamonds,
                points,
                good_deed,
                alive,
                eliminated
            )
            VALUES (
                $1,
                $2,
                $2,
                $3,
                10000,
                0,
                0,
                0,
                TRUE,
                FALSE
            )

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


# ============================================================
# 대기 게임 찾기
# ============================================================

async def get_waiting_game(
    channel_id: int
):

    connection = await get_db()

    try:

        return await connection.fetchrow(
            """
            SELECT *
            FROM games
            WHERE channel_id = $1
              AND status = 'waiting'
            ORDER BY id DESC
            LIMIT 1
            """,
            str(channel_id)
        )

    finally:
        await connection.close()


# ============================================================
# 대기 게임 생성
# ============================================================

async def create_waiting_game(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    host_id: int
):

    connection = await get_db()

    try:

        game = await connection.fetchrow(
            """
            INSERT INTO games (
                game_type,
                status,
                host_id,
                channel_id,
                current_phase,
                game_data
            )
            VALUES (
                'money_battle_royale',
                'waiting',
                $1,
                $2,
                'waiting',
                $3::jsonb
            )
            RETURNING *
            """,
            str(host_id),
            str(channel.id),
            '{"starting_money":10000,"min_players":4}'
        )

        return game

    finally:
        await connection.close()


# ============================================================
# 대기 게임 가져오기 / 생성
# ============================================================

async def get_or_create_waiting_game(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    user_id: int
):

    game = await get_waiting_game(
        channel.id
    )

    if game:
        return game

    return await create_waiting_game(
        guild,
        channel,
        user_id
    )


# ============================================================
# 참가자 수
# ============================================================

async def get_player_count(
    game_id: int
):

    connection = await get_db()

    try:

        return await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players
            WHERE game_id = $1
            """,
            game_id
        )

    finally:
        await connection.close()


# ============================================================
# 참가자 목록
# ============================================================

async def get_game_players(
    game_id: int
):

    connection = await get_db()

    try:

        return await connection.fetch(
            """
            SELECT *
            FROM game_players
            WHERE game_id = $1
            ORDER BY joined_at ASC
            """,
            game_id
        )

    finally:
        await connection.close()


# ============================================================
# 게임 참가
# ============================================================

async def join_game_player(
    user: discord.User,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel
):

    await get_or_create_player(
        user,
        guild
    )

    game = await get_or_create_waiting_game(
        guild,
        channel,
        user.id
    )

    connection = await get_db()

    try:

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

        await connection.execute(
            """
            INSERT INTO game_players (
                game_id,
                user_id,
                bet_amount,
                result,
                profit
            )
            VALUES (
                $1,
                $2,
                0,
                NULL,
                0
            )
            """,
            game["id"],
            str(user.id)
        )

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


# ============================================================
# 참가 취소
# ============================================================

async def cancel_game_player(
    user: discord.User,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel
):

    game = await get_waiting_game(
        channel.id
    )

    if not game:
        return None, False, 0

    connection = await get_db()

    try:

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

        await connection.execute(
            """
            DELETE FROM game_players
            WHERE game_id = $1
              AND user_id = $2
            """,
            game["id"],
            str(user.id)
        )

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


# ============================================================
# 게임 시작
# ============================================================

async def start_survival_game(
    game_id: int
):

    connection = await get_db()

    try:

        # 현재 게임 상태 확인
        game = await connection.fetchrow(
            """
            SELECT *
            FROM games
            WHERE id = $1
            FOR UPDATE
            """,
            game_id
        )

        if not game:
            raise RuntimeError(
                "게임을 찾을 수 없습니다."
            )

        if game["status"] != "waiting":
            raise RuntimeError(
                "이미 시작되었거나 종료된 게임입니다."
            )

        # 참가자 수
        count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players
            WHERE game_id = $1
            """,
            game_id
        )

        if count < MIN_PLAYERS:
            raise RuntimeError(
                f"최소 {MIN_PLAYERS}명이 필요합니다."
            )

        # 게임 시작
        await connection.execute(
            """
            UPDATE games
            SET
                status = 'playing',
                current_phase = 'survival',
                started_at = NOW(),
                game_data = jsonb_set(
                    COALESCE(game_data, '{}'::jsonb),
                    '{starting_money}',
                    '10000'::jsonb,
                    TRUE
                )
            WHERE id = $1
            """,
            game_id
        )

        # 참가자 생존 상태 초기화
        players = await connection.fetch(
            """
            SELECT user_id
            FROM game_players
            WHERE game_id = $1
            """,
            game_id
        )

        for player in players:

            await connection.execute(
                """
                UPDATE players
                SET
                    money = $1,
                    alive = TRUE,
                    eliminated = FALSE,
                    good_deed = 0,
                    updated_at = NOW()
                WHERE user_id = $2
                """,
                STARTING_MONEY,
                str(player["user_id"])
            )

        return count

    finally:
        await connection.close()


# ============================================================
# 테스트 게임 시작
# ============================================================

async def create_test_game(
    interaction: discord.Interaction
):

    await get_or_create_player(
        interaction.user,
        interaction.guild
    )

    connection = await get_db()

    try:

        game = await connection.fetchrow(
            """
            INSERT INTO games (
                game_type,
                status,
                host_id,
                channel_id,
                current_phase,
                started_at,
                game_data
            )
            VALUES (
                'money_battle_royale_test',
                'playing',
                $1,
                $2,
                'survival_test',
                NOW(),
                $3::jsonb
            )
            RETURNING *
            """,
            str(interaction.user.id),
            str(interaction.channel.id),
            '{"test":true,"starting_money":10000,"min_players":1}'
        )

        await connection.execute(
            """
            INSERT INTO game_players (
                game_id,
                user_id,
                bet_amount,
                result,
                profit
            )
            VALUES (
                $1,
                $2,
                0,
                NULL,
                0
            )
            """,
            game["id"],
            str(interaction.user.id)
        )

        await connection.execute(
            """
            UPDATE players
            SET
                money = $1,
                alive = TRUE,
                eliminated = FALSE,
                good_deed = 0,
                updated_at = NOW()
            WHERE user_id = $2
            """,
            STARTING_MONEY,
            str(interaction.user.id)
        )

        return game

    finally:
        await connection.close()


# ============================================================
# 대기방 UI
# ============================================================

class WaitingView(discord.ui.View):

    def __init__(
        self,
        game_id: int
    ):

        super().__init__(
            timeout=None
        )

        self.game_id = game_id


    # ========================================================
    # 참가
    # ========================================================

    @discord.ui.button(
        label="게임 참가",
        emoji="🎮",
        style=discord.ButtonStyle.success
    )
    async def join(
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
                    f"⚠️ 이미 참가 중입니다.\n\n"
                    f"👥 현재 참가자: **{count}명**",
                    ephemeral=True
                )

                return

            await interaction.response.send_message(
                f"🎉 게임 참가 완료!\n\n"
                f"👥 현재 참가자: **{count}명}\n"
                f"🎯 최소 참가 인원: **{MIN_PLAYERS}명**",
                ephemeral=True
            )

        except Exception as e:

            print("Waiting join error:")
            print(type(e).__name__)
            print(str(e))

            await interaction.response.send_message(
                f"🔴 참가 실패\n"
                f"`{type(e).__name__}`\n"
                f"{str(e)[:300]}",
                ephemeral=True
            )


    # ========================================================
    # 참가 취소
    # ========================================================

    @discord.ui.button(
        label="참가 취소",
        emoji="❌",
        style=discord.ButtonStyle.danger
    )
    async def cancel(
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
                    "⚠️ 현재 대기 중인 게임이 없습니다.",
                    ephemeral=True
                )

                return

            if not cancelled:

                await interaction.response.send_message(
                    f"⚠️ 참가 중이 아닙니다.\n\n"
                    f"👥 현재 참가자: **{count}명**",
                    ephemeral=True
                )

                return

            await interaction.response.send_message(
                f"❌ 참가를 취소했습니다.\n\n"
                f"👥 현재 참가자: **{count}명**",
                ephemeral=True
            )

        except Exception as e:

            print("Cancel error:")
            print(type(e).__name__)
            print(str(e))

            await interaction.response.send_message(
                "🔴 참가 취소 중 오류가 발생했습니다.",
                ephemeral=True
            )


    # ========================================================
    # 게임 시작
    # ========================================================

    @discord.ui.button(
        label="게임 시작",
        emoji="▶️",
        style=discord.ButtonStyle.primary
    )
    async def start(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            game = await get_waiting_game(
                interaction.channel.id
            )

            if not game:

                await interaction.response.send_message(
                    "⚠️ 대기 중인 게임이 없습니다.",
                    ephemeral=True
                )

                return

            # 방장 확인
            if str(game["host_id"]) != str(
                interaction.user.id
            ):

                await interaction.response.send_message(
                    "🔒 게임 시작은 방장만 할 수 있습니다.",
                    ephemeral=True
                )

                return

            count = await get_player_count(
                game["id"]
            )

            if count < MIN_PLAYERS:

                await interaction.response.send_message(
                    f"⚠️ 아직 게임을 시작할 수 없습니다.\n\n"
                    f"👥 현재 참가자: **{count}명**\n"
                    f"👥 최소 참가자: **{MIN_PLAYERS}명**",
                    ephemeral=True
                )

                return

            await interaction.response.send_message(
                "🎮 게임 시작 준비!\n\n"
                "⏳ **3초 후 게임이 시작됩니다!**"
            )

            for number in [3, 2, 1]:

                await asyncio.sleep(1)

                try:
                    await interaction.edit_original_response(
                        content=(
                            "🎮 **MONEY BATTLE ROYALE**\n\n"
                            f"🔥 게임 시작까지 **{number}초**!"
                        )
                    )
                except Exception:
                    pass

            await start_survival_game(
                game["id"]
            )

            await interaction.edit_original_response(
                content=(
                    "💰 **MONEY BATTLE ROYALE**\n\n"
                    "🎉 **게임이 시작되었습니다!**\n\n"
                    f"👥 참가자: **{count}명**\n"
                    f"🪙 시작 자금: **{STARTING_MONEY:,} 코인**\n\n"
                    "☠️ 이제 서바이벌이 시작됩니다."
                ),
                view=None
            )

        except Exception as e:

            print("Game start error:")
            print(type(e).__name__)
            print(str(e))

            try:

                if interaction.response.is_done():

                    await interaction.followup.send(
                        f"🔴 게임 시작 실패\n"
                        f"오류: `{type(e).__name__}`\n"
                        f"내용: `{str(e)[:500]}`",
                        ephemeral=True
                    )

                else:

                    await interaction.response.send_message(
                        f"🔴 게임 시작 실패\n"
                        f"오류: `{type(e).__name__}`\n"
                        f"내용: `{str(e)[:500]}`",
                        ephemeral=True
                    )

            except Exception:
                pass


# ============================================================
# 메인 메뉴
# ============================================================

class MainView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=300
        )


    # ========================================================
    # 내 정보
    # ========================================================

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
                f"🪙 코인: **{player['money']:,}**\n"
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


    # ========================================================
    # 게임 참가
    # ========================================================

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

            players = await get_game_players(
                game["id"]
            )

            names = []

            for player in players:

                member = interaction.guild.get_member(
                    int(player["user_id"])
                )

                if member:
                    names.append(
                        f"• {member.display_name}"
                    )

            player_list = "\n".join(names)

            if not player_list:
                player_list = "없음"

            embed = discord.Embed(
                title="🎮 MONEY BATTLE ROYALE",
                description=(
                    "게임 참가 대기 중입니다.\n\n"
                    f"👥 참가자: **{count}명**\n"
                    f"🎯 최소 인원: **{MIN_PLAYERS}명**\n\n"
                    f"**참가자 목록**\n"
                    f"{player_list}"
                )
            )

            host_name = interaction.guild.get_member(
                int(game["host_id"])
            )

            if host_name:

                embed.add_field(
                    name="👑 방장",
                    value=host_name.mention,
                    inline=False
                )

            await interaction.response.send_message(
                embed=embed,
                view=WaitingView(game["id"])
            )

        except Exception as e:

            print("Join main error:")
            print(type(e).__name__)
            print(str(e))

            await interaction.response.send_message(
                f"🔴 게임 참가 중 오류가 발생했습니다.\n"
                f"`{type(e).__name__}`\n"
                f"{str(e)[:300]}",
                ephemeral=True
            )


    # ========================================================
    # 게임 설명
    # ========================================================

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
                "돈을 벌고 게임을 플레이하며\n"
                "최후의 1인이 되는 서바이벌 게임입니다."
            )
        )

        embed.add_field(
            name="👥 최소 인원",
            value=f"{MIN_PLAYERS}명",
            inline=True
        )

        embed.add_field(
            name="🪙 시작 자금",
            value=f"{STARTING_MONEY:,} 코인",
            inline=True
        )

        embed.add_field(
            name="🏆 목표",
            value="최후의 1인이 되기",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


    # ========================================================
    # 닫기
    # ========================================================

    @discord.ui.button(
        label="닫기",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content="메인 메뉴를 닫았습니다.",
            embed=None,
            view=None
        )


# ============================================================
# /메인
# ============================================================

@bot.tree.command(
    name="메인",
    description="머니 배틀로얄 메인 메뉴를 엽니다."
)
async def main_menu(
    interaction: discord.Interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            "🔴 디스코드 서버에서 사용해주세요.",
            ephemeral=True
        )

        return

    try:

        player = await get_or_create_player(
            interaction.user,
            interaction.guild
        )

        embed = discord.Embed(
            title="💰 MONEY BATTLE ROYALE",
            description=(
                "━━━━━━━━━━━━━━━━━━\n"
                "💰 **MONEY BATTLE ROYALE**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "돈을 벌고,\n"
                "게임을 플레이하고,\n"
                "마지막까지 살아남으세요!\n\n"
                f"🪙 코인: **{player['money']:,}**\n"
                f"💎 다이아: **{player['diamonds']:,}**\n"
                f"😇 선행 포인트: **{player['good_deed']:,}**"
            )
        )

        await interaction.response.send_message(
            embed=embed,
            view=MainView(),
            ephemeral=True
        )

    except Exception as e:

        print("Main menu error:")
        print(type(e).__name__)
        print(str(e))

        await interaction.response.send_message(
            f"🔴 메인 메뉴 오류\n"
            f"`{type(e).__name__}`\n"
            f"{str(e)[:500]}",
            ephemeral=True
        )


# ============================================================
# /게임테스트
# ============================================================

@bot.tree.command(
    name="게임테스트",
    description="1인으로 실제 게임 시작 화면을 테스트합니다."
)
async def game_test(
    interaction: discord.Interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            "🔴 디스코드 서버에서 사용해주세요.",
            ephemeral=True
        )

        return

    try:

        await interaction.response.send_message(
            "🧪 **테스트 게임 준비 중...**"
        )

        for number in [3, 2, 1]:

            await asyncio.sleep(1)

            await interaction.edit_original_response(
                content=(
                    "🧪 **MONEY BATTLE ROYALE TEST**\n\n"
                    f"🔥 게임 시작까지 **{number}초**!"
                )
            )

        game = await create_test_game(
            interaction
        )

        await interaction.edit_original_response(
            content=(
                "🧪 **MONEY BATTLE ROYALE TEST**\n\n"
                "🎉 **테스트 게임이 시작되었습니다!**\n\n"
                "👥 참가자: **1명**\n"
                f"🪙 시작 자금: **{STARTING_MONEY:,} 코인**\n\n"
                f"🎮 Game ID: **{game['id']}**\n"
                "☠️ 현재 단계: **SURVIVAL TEST**"
            )
        )

        print(
            f"[GAME TEST] game_id={game['id']} "
            f"user_id={interaction.user.id}"
        )

    except Exception as e:

        print("Game test error:")
        print(type(e).__name__)
        print(str(e))

        if interaction.response.is_done():

            await interaction.followup.send(
                f"🔴 게임 테스트 실패\n"
                f"`{type(e).__name__}`\n"
                f"{str(e)[:500]}",
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                f"🔴 게임 테스트 실패\n"
                f"`{type(e).__name__}`\n"
                f"{str(e)[:500]}",
                ephemeral=True
            )


# ============================================================
# /dbtest
# ============================================================

@bot.tree.command(
    name="dbtest",
    description="Supabase 데이터베이스 연결을 테스트합니다."
)
async def dbtest(
    interaction: discord.Interaction
):

    try:

        connection = await get_db()

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

        print("DB test error:")
        print(type(e).__name__)
        print(str(e))

        await interaction.response.send_message(
            f"🔴 DB 연결 실패\n"
            f"`{type(e).__name__}`\n"
            f"{str(e)[:500]}",
            ephemeral=True
        )


# ============================================================
# on_ready
# ============================================================

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

    try:

        connection = await get_db()

        result = await connection.fetchval(
            "SELECT 1;"
        )

        await connection.close()

        if result == 1:
            print(
                "Database connected successfully!"
            )

    except Exception as e:

        print(
            "Database connection failed:"
        )

        print(
            type(e).__name__
        )

        print(
            str(e)
        )


# ============================================================
# 실행
# ============================================================

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
