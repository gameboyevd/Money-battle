import os
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands
import asyncpg
from collections import defaultdict

# ============================================================
# 설정
# ============================================================

MIN_PLAYERS = 4
STARTING_MONEY = 10_000

# 알바 쿨타임
JOB_COOLDOWN_SECONDS = 5 * 60

# 현재 실행 중인 Render 인스턴스의 알바 쿨타임
# {user_id: datetime}
job_cooldowns = {}


# ============================================================
# 중복 클릭 방지
# ============================================================

_user_locks = defaultdict(asyncio.Lock)


class UserActionLock:

    def __init__(self, user_id):
        self.user_id = user_id
        self.lock = _user_locks[user_id]

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.lock.release()


def action_lock(user_id):
    return UserActionLock(user_id)

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

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(
        f"HTTP server started on port {port}"
    )

    server.serve_forever()


# ============================================================
# Discord
# ============================================================

intents = discord.Intents.default()

intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# DB
# ============================================================

async def get_db():

    database_url = os.environ.get(
        "DATABASE_URL"
    )

    if not database_url:

        raise RuntimeError(
            "DATABASE_URL이 설정되지 않았습니다."
        )

    return await asyncpg.connect(
        database_url
    )


# ============================================================
# 플레이어 등록 / 조회
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

        return await connection.fetchrow(
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
                $4,
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
            user.name,
            STARTING_MONEY
        )

    finally:

        await connection.close()


# ============================================================
# 대기 게임 조회
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
    guild,
    channel,
    host_id
):

    connection = await get_db()

    try:

        return await connection.fetchrow(
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
            (
                '{"starting_money":10000,'
                '"min_players":4}'
            )
        )

    finally:

        await connection.close()


# ============================================================
# 대기 게임 가져오기 / 생성
# ============================================================

async def get_or_create_waiting_game(
    guild,
    channel,
    user_id
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
    game_id
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
    game_id
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
    user,
    guild,
    channel
):

    await get_or_create_player(
        user,
        guild
    )

    connection = await get_db()

    try:

        async with connection.transaction():

            # --------------------------------------------
            # 현재 대기 게임을 DB Lock과 함께 확인
            # --------------------------------------------

            game = await connection.fetchrow(
                """
                SELECT *
                FROM games
                WHERE channel_id = $1
                  AND status = 'waiting'
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                str(channel.id)
            )

            # --------------------------------------------
            # 대기 게임이 없으면 새로 생성
            # --------------------------------------------

            if not game:

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
                    str(user.id),
                    str(channel.id),
                    (
                        '{"starting_money":10000,'
                        '"min_players":4}'
                    )
                )

            # --------------------------------------------
            # 이미 참가했는지 확인
            # --------------------------------------------

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

                return (
                    game,
                    False,
                    count
                )

            # --------------------------------------------
            # 참가
            #
            # UNIQUE(game_id, user_id)가 있기 때문에
            # 동시에 여러 번 눌러도 중복 참가 방지
            # --------------------------------------------

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
                ON CONFLICT (game_id, user_id)
                DO NOTHING
                """,
                game["id"],
                str(user.id)
            )

            # --------------------------------------------
            # 최종 참가자 수
            # --------------------------------------------

            count = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM game_players
                WHERE game_id = $1
                """,
                game["id"]
            )

            return (
                game,
                True,
                count
            )

    finally:

        await connection.close()

# ============================================================
# 게임 참가 취소
# ============================================================

async def cancel_game_player(
    user,
    guild,
    channel
):

    connection = await get_db()

    try:

        async with connection.transaction():

            # 현재 대기 게임을 잠금
            game = await connection.fetchrow(
                """
                SELECT *
                FROM games
                WHERE channel_id = $1
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                str(channel.id)
            )

            if not game:

                return (
                    None,
                    False,
                    0
                )

            # --------------------------------------------
            # 게임이 이미 시작됐으면 참가 취소 금지
            # --------------------------------------------

            if game["status"] != "waiting":

                count = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM game_players
                    WHERE game_id = $1
                    """,
                    game["id"]
                )

                return (
                    game,
                    False,
                    count
                )

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

                return (
                    game,
                    False,
                    count
                )

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

            return (
                game,
                True,
                count
            )

    finally:

        await connection.close()
# ============================================================
# 게임 시작
# ============================================================

async def start_survival_game(
    game_id
):

    connection = await get_db()

    try:

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

        await connection.execute(
            """
            UPDATE games
            SET
                status = 'playing',
                current_phase = 'survival',
                started_at = NOW(),
                game_data = jsonb_set(
                    COALESCE(
                        game_data,
                        '{}'::jsonb
                    ),
                    '{starting_money}',
                    '10000'::jsonb,
                    TRUE
                )
            WHERE id = $1
            """,
            game_id
        )

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
# 테스트 게임 생성
# ============================================================

async def create_test_game(
    interaction
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
            (
                '{"test":true,'
                '"starting_money":10000,'
                '"min_players":1}'
            )
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
# 테스트 게임 화면 데이터
# ============================================================

async def get_test_game_screen(
    game_id,
    user_id
):

    connection = await get_db()

    try:

        game = await connection.fetchrow(
            """
            SELECT *
            FROM games
            WHERE id = $1
            """,
            game_id
        )

        if not game:

            raise RuntimeError(
                "게임을 찾을 수 없습니다."
            )

        player = await connection.fetchrow(
            """
            SELECT *
            FROM players
            WHERE user_id = $1
            LIMIT 1
            """,
            str(user_id)
        )

        if not player:

            raise RuntimeError(
                "플레이어를 찾을 수 없습니다."
            )

        count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players
            WHERE game_id = $1
            """,
            game_id
        )

        alive_count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players gp
            JOIN players p
              ON p.user_id = gp.user_id
            WHERE gp.game_id = $1
              AND p.alive = TRUE
            """,
            game_id
        )

        return (
            game,
            player,
            count,
            alive_count
        )

    finally:

        await connection.close()


# ============================================================
# 대기방 View
# ============================================================
# ============================================================
# 안전한 View
# ============================================================

class SafeView(discord.ui.View):

    def __init__(self, timeout=300):
        super().__init__(timeout=timeout)

    async def on_timeout(self):

        for item in self.children:

            if isinstance(item, discord.ui.Button):
                item.disabled = True

        # Discord 메시지를 직접 수정하지 않음.
        # ephemeral 메시지는 봇이 나중에 안정적으로 찾기 어려울 수 있음.

class WaitingView(SafeView):

    def __init__(
        self,
        game_id
    ):

        super().__init__(
            timeout=600
        )

        self.game_id = game_id

    # --------------------------------------------------------
    # 게임 참가
    # --------------------------------------------------------

    @discord.ui.button(
        label="게임 참가",
        emoji="🎮",
        style=discord.ButtonStyle.success
    )
    async def join(
    self,
    interaction,
    button
):

    user_id = interaction.user.id

    if _user_locks[user_id].locked():

        await interaction.response.send_message(
            "⏳ 처리 중입니다. 잠시만 기다려주세요.",
            ephemeral=True
        )

        return

    async with action_lock(user_id):

        try:

            game = await get_waiting_game(
                interaction.channel.id
            )

            if not game:

                await interaction.response.send_message(
                    "⚠️ 이 대기방은 더 이상 존재하지 않습니다.",
                    ephemeral=True
                )

                return

            if game["id"] != self.game_id:

                await interaction.response.send_message(
                    "⚠️ 이미 다른 게임 대기방으로 변경되었습니다.",
                    ephemeral=True
                )

                return

            (
                game,
                joined,
                count
            ) = await join_game_player(
                interaction.user,
                interaction.guild,
                interaction.channel
            )

            if not joined:

                await interaction.response.send_message(
                    (
                        "⚠️ 이미 참가 중입니다.\n\n"
                        f"👥 현재 참가자: **{count}명**"
                    ),
                    ephemeral=True
                )

                return

            await interaction.response.send_message(
                (
                    "🎉 게임 참가 완료!\n\n"
                    f"👥 현재 참가자: **{count}명**\n"
                    f"🎯 최소 참가 인원: **{MIN_PLAYERS}명**"
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "Waiting join error:",
                type(e).__name__,
                str(e)
            )

            if not interaction.response.is_done():

                await interaction.response.send_message(
                    (
                        "🔴 참가 실패\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:300]}"
                    ),
                    ephemeral=True
                )
    # --------------------------------------------------------
    # 참가 취소
    # --------------------------------------------------------

    @discord.ui.button(
        label="참가 취소",
        emoji="❌",
        style=discord.ButtonStyle.danger
    )
    async def cancel(
        self,
        interaction,
        button
    ):

        try:

            (
                game,
                cancelled,
                count
            ) = await cancel_game_player(
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
                    (
                        "⚠️ 참가 중이 아닙니다.\n\n"
                        f"👥 현재 참가자: **{count}명**"
                    ),
                    ephemeral=True
                )

                return

            await interaction.response.send_message(
                (
                    "❌ 참가를 취소했습니다.\n\n"
                    f"👥 현재 참가자: **{count}명**"
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "Cancel error:",
                type(e).__name__,
                str(e)
            )

            await interaction.response.send_message(
                "🔴 참가 취소 중 오류가 발생했습니다.",
                ephemeral=True
            )

    # --------------------------------------------------------
    # 게임 시작
    # --------------------------------------------------------

    @discord.ui.button(
        label="게임 시작",
        emoji="▶️",
        style=discord.ButtonStyle.primary
    )
    async def start(
        self,
        interaction,
        button
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
                    (
                        "⚠️ 아직 게임을 시작할 수 없습니다.\n\n"
                        f"👥 현재 참가자: **{count}명**\n"
                        f"🎯 최소 참가자: **{MIN_PLAYERS}명**"
                    ),
                    ephemeral=True
                )

                return

            await interaction.response.send_message(
                (
                    "🎮 게임 시작 준비!\n\n"
                    "⏳ **3초 후 게임이 시작됩니다!**"
                )
            )

            for number in [3, 2, 1]:

                await asyncio.sleep(1)

                await interaction.edit_original_response(
                    content=(
                        "🎮 **MONEY BATTLE ROYALE**\n\n"
                        f"🔥 게임 시작까지 **{number}초**!"
                    )
                )

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

            print(
                "Game start error:",
                type(e).__name__,
                str(e)
            )

            if interaction.response.is_done():

                await interaction.followup.send(
                    (
                        "🔴 게임 시작 실패\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:500]}"
                    ),
                    ephemeral=True
                )

            else:

                await interaction.response.send_message(
                    (
                        "🔴 게임 시작 실패\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:500]}"
                    ),
                    ephemeral=True
                )


# ============================================================
# 알바 시스템
# ============================================================

JOBS = {
    "청소": {
        "emoji": "🧹",
        "reward": 20_000
    },
    "택배": {
        "emoji": "📦",
        "reward": 22_000
    },
    "과녁": {
        "emoji": "🎯",
        "reward": 22_000
    },
    "패스트푸드": {
        "emoji": "🍔",
        "reward": 25_000
    },
    "배달": {
        "emoji": "🏃",
        "reward": 25_000
    },
    "주방": {
        "emoji": "🍳",
        "reward": 28_000
    },
    "데이터 입력": {
        "emoji": "🧠",
        "reward": 30_000
    },
    "낚시": {
        "emoji": "🎣",
        "reward": 30_000
    }
}


def get_job_remaining(
    user_id: int
):

    cooldown = job_cooldowns.get(
        user_id
    )

    if cooldown is None:

        return 0

    now = datetime.utcnow()

    if now >= cooldown:

        job_cooldowns.pop(
            user_id,
            None
        )

        return 0

    return int(
        (cooldown - now).total_seconds()
    )


def format_seconds(
    seconds: int
):

    minutes = seconds // 60
    seconds %= 60

    if minutes > 0:

        return (
            f"{minutes}분 "
            f"{seconds}초"
        )

    return f"{seconds}초"


# ============================================================
# 알바 View
# ============================================================

class JobView(
    discord.ui.View
):

    def __init__(
        self,
        game_id
    ):

        super().__init__(
            timeout=300
        )

        self.game_id = game_id

    async def do_job(
        self,
        interaction: discord.Interaction,
        job_name: str
    ):

        try:

            remaining = get_job_remaining(
                interaction.user.id
            )

            if remaining > 0:

                await interaction.response.send_message(
                    (
                        "⏳ **아직 알바를 할 수 없습니다.**\n\n"
                        f"🕐 남은 시간: **{format_seconds(remaining)}**"
                    ),
                    ephemeral=True
                )

                return

            job = JOBS[job_name]

            player = await get_or_create_player(
                interaction.user,
                interaction.guild
            )

            reward = job["reward"]

            connection = await get_db()

            try:

                new_money = await connection.fetchval(
                    """
                    UPDATE players
                    SET
                        money = money + $1,
                        updated_at = NOW()
                    WHERE server_id = $2
                      AND user_id = $3
                    RETURNING money
                    """,
                    reward,
                    str(interaction.guild.id),
                    str(interaction.user.id)
                )

            finally:

                await connection.close()

            if new_money is None:

                raise RuntimeError(
                    "플레이어 정보를 업데이트하지 못했습니다."
                )

            job_cooldowns[
                interaction.user.id
            ] = (
                datetime.utcnow()
                + timedelta(
                    seconds=JOB_COOLDOWN_SECONDS
                )
            )

            await interaction.response.send_message(
                (
                    f"{job['emoji']} **{job_name} 알바 완료!**\n\n"
                    f"💰 획득: **+{reward:,} 코인**\n"
                    f"🪙 현재 코인: **{new_money:,} 코인**\n\n"
                    "⏳ 다음 알바까지 **5분**"
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "Job execution error:",
                type(e).__name__,
                str(e)
            )

            if interaction.response.is_done():

                await interaction.followup.send(
                    (
                        "🔴 알바 처리 오류\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:300]}"
                    ),
                    ephemeral=True
                )

            else:

                await interaction.response.send_message(
                    (
                        "🔴 알바 처리 오류\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:300]}"
                    ),
                    ephemeral=True
                )

    @discord.ui.button(
        label="청소",
        emoji="🧹",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def cleaning(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "청소"
        )

    @discord.ui.button(
        label="택배",
        emoji="📦",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def package(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "택배"
        )

    @discord.ui.button(
        label="과녁",
        emoji="🎯",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def target(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "과녁"
        )

    @discord.ui.button(
        label="패스트푸드",
        emoji="🍔",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def fast_food(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "패스트푸드"
        )

    @discord.ui.button(
        label="배달",
        emoji="🏃",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def delivery(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "배달"
        )

    @discord.ui.button(
        label="주방",
        emoji="🍳",
        style=discord.ButtonStyle.primary,
        row=1
    )
    async def kitchen(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "주방"
        )

    @discord.ui.button(
        label="데이터 입력",
        emoji="🧠",
        style=discord.ButtonStyle.primary,
        row=2
    )
    async def data_input(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "데이터 입력"
        )

    @discord.ui.button(
        label="낚시",
        emoji="🎣",
        style=discord.ButtonStyle.primary,
        row=2
    )
    async def fishing(
        self,
        interaction,
        button
    ):

        await self.do_job(
            interaction,
            "낚시"
        )

    @discord.ui.button(
        label="닫기",
        emoji="❌",
        style=discord.ButtonStyle.secondary,
        row=3
    )
    async def close(
        self,
        interaction,
        button
    ):

        await interaction.response.edit_message(
            content="🧑‍💼 알바 메뉴를 닫았습니다.",
            embed=None,
            view=None
        )


# ============================================================
# 실제 게임 화면
# ============================================================

class SurvivalGameView(SafeView):

    def __init__(
        self,
        game_id
    ):

        super().__init__(
            timeout=1800
        )

        self.game_id = game_id

    # --------------------------------------------------------
    # 게임
    # --------------------------------------------------------

    @discord.ui.button(
        label="게임",
        emoji="🎮",
        style=discord.ButtonStyle.success,
        row=0
    )
    async def games(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            (
                "🎮 **게임 메뉴**\n\n"
                "🃏 블랙잭\n"
                "🃏 에이스 브레이커\n"
                "🎲 미니 친치로\n"
                "🧠 인디언 포커\n"
                "🎡 룰렛\n"
                "💣 폭탄 룰렛\n"
                "🔢 홀짝\n"
                "🎭 야바위\n"
                "🏇 경마\n\n"
                "⚠️ 게임 기능은 순차적으로 연결됩니다."
            ),
            ephemeral=True
        )

    # --------------------------------------------------------
    # 알바
    # --------------------------------------------------------

    @discord.ui.button(
        label="알바",
        emoji="🧑‍💼",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def jobs(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            player = await get_or_create_player(
                interaction.user,
                interaction.guild
            )

            remaining = get_job_remaining(
                interaction.user.id
            )

            if remaining > 0:

                cooldown_text = (
                    f"⏳ 현재 알바 쿨타임: "
                    f"**{format_seconds(remaining)}**"
                )

            else:

                cooldown_text = (
                    "🟢 지금 바로 알바할 수 있습니다."
                )

            embed = discord.Embed(
                title="🧑‍💼 알바",
                description=(
                    "알바를 해서 코인을 벌 수 있습니다.\n\n"
                    f"{cooldown_text}\n\n"
                    "한 번 일을 하면 **5분 동안** "
                    "다시 일할 수 없습니다."
                )
            )

            for job_name, job in JOBS.items():

                embed.add_field(
                    name=(
                        f"{job['emoji']} "
                        f"{job_name}"
                    ),
                    value=(
                        f"{job['reward']:,} 코인"
                    ),
                    inline=True
                )

            embed.set_footer(
                text=(
                    f"현재 코인: "
                    f"{player['money']:,}"
                )
            )

            await interaction.response.send_message(
                embed=embed,
                view=JobView(
                    self.game_id
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "Job menu error:",
                type(e).__name__,
                str(e)
            )

            await interaction.response.send_message(
                (
                    "🔴 알바 메뉴 오류\n"
                    f"`{type(e).__name__}`\n"
                    f"{str(e)[:300]}"
                ),
                ephemeral=True
            )

    # --------------------------------------------------------
    # 상점
    # --------------------------------------------------------

    @discord.ui.button(
        label="상점",
        emoji="🏪",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def shop(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            (
                "🏪 **일반 상점**\n\n"
                "👁️ 정찰권 — 300,000 코인\n"
                "🪣 빨대 쪼옵 — 600,000 코인\n"
                "🎟️ 이벤트 참가권 — 500,000 코인\n"
                "⏳ 시간 연장권 — 1,000,000 코인\n"
                "🎁 랜덤박스 — 400,000 코인~\n"
                "🎭 밑장빼기권 — 800,000 코인"
            ),
            ephemeral=True
        )

    # --------------------------------------------------------
    # 기부
    # --------------------------------------------------------

    @discord.ui.button(
        label="기부",
        emoji="😇",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def donate(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            (
                "😇 **기부 시스템**\n\n"
                "현재 꼴등에게 코인을 기부할 수 있습니다.\n"
                "기부 기능은 다음 단계에서 연결합니다."
            ),
            ephemeral=True
        )

    # --------------------------------------------------------
    # 아이템
    # --------------------------------------------------------

    @discord.ui.button(
        label="아이템",
        emoji="🎒",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def items(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            (
                "🎒 **보유 아이템**\n\n"
                "현재 보유한 아이템을 표시합니다.\n"
                "아이템 기능은 다음 단계에서 연결합니다."
            ),
            ephemeral=True
        )

    # --------------------------------------------------------
    # 내 정보
    # --------------------------------------------------------

    @discord.ui.button(
        label="내 정보",
        emoji="👤",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def info(
        self,
        interaction,
        button
    ):

        try:

            player = await get_or_create_player(
                interaction.user,
                interaction.guild
            )

            await interaction.response.send_message(
                (
                    "👤 **내 정보**\n\n"
                    f"🪙 코인: **{player['money']:,}**\n"
                    f"💎 다이아: **{player['diamonds']:,}**\n"
                    f"⭐ 포인트: **{player['points']:,}**\n"
                    f"😇 선행 포인트: **{player['good_deed']:,}**"
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "Game info error:",
                type(e).__name__,
                str(e)
            )

            await interaction.response.send_message(
                "🔴 정보를 불러오지 못했습니다.",
                ephemeral=True
            )


# ============================================================
# 게임 강제종료
# ============================================================

async def force_end_game(
    game_id: int
):

    connection = await get_db()

    try:

        game = await connection.fetchrow(
            """
            SELECT *
            FROM games
            WHERE id = $1
            """,
            game_id
        )

        if not game:

            raise RuntimeError(
                "게임을 찾을 수 없습니다."
            )

        if game["status"] == "ended":

            return (
                False,
                "이미 종료된 게임입니다."
            )

        await connection.execute(
            """
            UPDATE games
            SET
                status = 'ended',
                current_phase = 'ended',
                ended_at = NOW(),
                game_data = jsonb_set(
                    COALESCE(
                        game_data,
                        '{}'::jsonb
                    ),
                    '{force_ended}',
                    'true'::jsonb,
                    TRUE
                )
            WHERE id = $1
            """,
            game_id
        )

        await connection.execute(
            """
            UPDATE game_players
            SET
                ended_reason = 'force_ended',
                result = NULL,
                profit = 0
            WHERE game_id = $1
            """,
            game_id
        )

        await connection.execute(
            """
            UPDATE players
            SET
                alive = FALSE,
                eliminated = TRUE,
                updated_at = NOW()
            WHERE user_id IN (
                SELECT user_id
                FROM game_players
                WHERE game_id = $1
            )
            """,
            game_id
        )

        return (
            True,
            "게임이 강제 종료되었습니다."
        )

    finally:

        await connection.close()


# ============================================================
# /게임종료
# ============================================================

@bot.tree.command(
    name="게임종료",
    description="현재 진행 중인 게임을 강제로 종료합니다."
)
async def force_end_game_command(
    interaction: discord.Interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            "🔴 디스코드 서버에서 사용해주세요.",
            ephemeral=True
        )

        return

    try:

        connection = await get_db()

        try:

            game = await connection.fetchrow(
                """
                SELECT *
                FROM games
                WHERE channel_id = $1
                  AND status = 'playing'
                ORDER BY id DESC
                LIMIT 1
                """,
                str(interaction.channel.id)
            )

        finally:

            await connection.close()

        if not game:

            await interaction.response.send_message(
                (
                    "⚠️ 현재 이 채널에서 "
                    "**진행 중인 게임**이 없습니다.\n\n"
                    "💡 대기 중인 게임은 "
                    "`/게임종료`로 종료되지 않습니다."
                ),
                ephemeral=True
            )

            return

        if str(game["host_id"]) != str(
            interaction.user.id
        ):

            await interaction.response.send_message(
                (
                    "🔒 게임 강제종료는 "
                    "**방장만** 사용할 수 있습니다."
                ),
                ephemeral=True
            )

            return

        view = ForceEndConfirmView(
            game["id"]
        )

        await interaction.response.send_message(
            (
                "⚠️ **게임 강제종료**\n\n"
                f"🎮 Game ID: **{game['id']}**\n"
                "📌 현재 상태: **PLAYING**\n\n"
                "정말 현재 게임을 강제로 종료하시겠습니까?\n\n"
                "⚠️ 종료된 게임은 다시 시작할 수 없습니다.\n"
                "⚠️ 모든 참가자는 해당 게임에서 탈락 처리됩니다."
            ),
            view=view,
            ephemeral=True
        )

    except Exception as e:

        print(
            "Force end command error:",
            type(e).__name__,
            str(e)
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                (
                    "🔴 게임 종료 중 오류가 발생했습니다.\n"
                    f"`{type(e).__name__}`\n"
                    f"{str(e)[:500]}"
                ),
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                (
                    "🔴 게임 종료 중 오류가 발생했습니다.\n"
                    f"`{type(e).__name__}`\n"
                    f"{str(e)[:500]}"
                ),
                ephemeral=True
            )


# ============================================================
# 강제종료 확인 UI
# ============================================================

class ForceEndConfirmView(
    discord.ui.View
):

    def __init__(
        self,
        game_id: int
    ):

        super().__init__(
            timeout=30
        )

        self.game_id = game_id

    @discord.ui.button(
        label="게임 종료",
        emoji="🛑",
        style=discord.ButtonStyle.danger
    )
    async def confirm(
        self,
        interaction,
        button
    ):

        try:

            connection = await get_db()

            try:

                game = await connection.fetchrow(
                    """
                    SELECT *
                    FROM games
                    WHERE id = $1
                    """,
                    self.game_id
                )

            finally:

                await connection.close()

            if not game:

                await interaction.response.edit_message(
                    content="⚠️ 게임을 찾을 수 없습니다.",
                    view=None
                )

                return

            if game["status"] != "playing":

                await interaction.response.edit_message(
                    content="⚠️ 이 게임은 더 이상 진행 중이 아닙니다.",
                    view=None
                )

                return

            if str(game["host_id"]) != str(
                interaction.user.id
            ):

                await interaction.response.edit_message(
                    content="🔒 게임 종료 권한이 없습니다.",
                    view=None
                )

                return

            (
                success,
                message
            ) = await force_end_game(
                self.game_id
            )

            if not success:

                await interaction.response.edit_message(
                    content=f"⚠️ {message}",
                    view=None
                )

                return

            await interaction.response.edit_message(
                content=(
                    "🛑 **게임이 강제 종료되었습니다.**\n\n"
                    f"🎮 Game ID: **{self.game_id}**\n"
                    "📌 상태: **ENDED**\n"
                    "👥 참가자: 모두 게임 종료 처리됨"
                ),
                view=None
            )

        except Exception as e:

            print(
                "Force end confirmation error:",
                type(e).__name__,
                str(e)
            )

            if interaction.response.is_done():

                await interaction.followup.send(
                    (
                        "🔴 게임 강제종료 중 오류가 발생했습니다.\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:500]}"
                    ),
                    ephemeral=True
                )

            else:

                await interaction.response.edit_message(
                    content=(
                        "🔴 게임 강제종료 중 오류가 발생했습니다.\n"
                        f"`{type(e).__name__}`\n"
                        f"{str(e)[:500]}"
                    ),
                    view=None
                )

    @discord.ui.button(
        label="취소",
        emoji="❌",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self,
        interaction,
        button
    ):

        await interaction.response.edit_message(
            content="✅ 게임 강제종료를 취소했습니다.",
            view=None
        )


# ============================================================
# 메인 메뉴
# ============================================================

class MainView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=300
        )

    # --------------------------------------------------------
    # 내 정보
    # --------------------------------------------------------

    @discord.ui.button(
        label="내 정보",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def my_info(
        self,
        interaction,
        button
    ):

        try:

            player = await get_or_create_player(
                interaction.user,
                interaction.guild
            )

            await interaction.response.send_message(
                (
                    f"👤 **{interaction.user.display_name}님의 정보**\n\n"
                    f"🪙 코인: **{player['money']:,}**\n"
                    f"💎 다이아: **{player['diamonds']:,}**\n"
                    f"⭐ 포인트: **{player['points']:,}**\n"
                    f"😇 선행 포인트: **{player['good_deed']:,}**"
                ),
                ephemeral=True
            )

        except Exception as e:

            print(
                "My info error:",
                type(e).__name__,
                str(e)
            )

            await interaction.response.send_message(
                "🔴 정보를 불러오는 중 오류가 발생했습니다.",
                ephemeral=True
            )

    # --------------------------------------------------------
    # 게임 참가
    # --------------------------------------------------------

    @discord.ui.button(
        label="게임 참가",
        emoji="🎮",
        style=discord.ButtonStyle.success,
        row=1
    )
    async def join_game(
        self,
        interaction,
        button
    ):

        try:

            (
                game,
                joined,
                count
            ) = await join_game_player(
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

            player_list = "\n".join(
                names
            )

            if not player_list:

                player_list = "없음"

            embed = discord.Embed(
                title="🎮 MONEY BATTLE ROYALE",
                description=(
                    "게임 참가 대기 중입니다.\n\n"
                    f"👥 참가자: **{count}명**\n"
                    f"🎯 최소 인원: **{MIN_PLAYERS}명**\n\n"
                    "**참가자 목록**\n"
                    f"{player_list}"
                )
            )

            host = interaction.guild.get_member(
                int(game["host_id"])
            )

            if host:

                embed.add_field(
                    name="👑 방장",
                    value=host.mention,
                    inline=False
                )

            await interaction.response.send_message(
                embed=embed,
                view=WaitingView(
                    game["id"]
                )
            )

        except Exception as e:

            print(
                "Join main error:",
                type(e).__name__,
                str(e)
            )

            await interaction.response.send_message(
                (
                    "🔴 게임 참가 중 오류가 발생했습니다.\n"
                    f"`{type(e).__name__}`\n"
                    f"{str(e)[:300]}"
                ),
                ephemeral=True
            )

    # --------------------------------------------------------
    # 게임 설명
    # --------------------------------------------------------

    @discord.ui.button(
        label="게임 설명",
        emoji="📖",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def game_info(
        self,
        interaction,
        button
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

    # --------------------------------------------------------
    # 닫기
    # --------------------------------------------------------

    @discord.ui.button(
        label="닫기",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def close(
        self,
        interaction,
        button
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
async def main_menu(interaction):

    if interaction.guild is None:
        await interaction.response.send_message(
            "🔴 디스코드 서버에서 사용해주세요.",
            ephemeral=True
        )
        return

    try:

        # 현재 채널에서 진행 중인 게임 확인
        connection = await get_db()

        try:
            game = await connection.fetchrow(
                """
                SELECT *
                FROM games
                WHERE channel_id = $1
                  AND status = 'playing'
                ORDER BY id DESC
                LIMIT 1
                """,
                str(interaction.channel.id)
            )
        finally:
            await connection.close()

        # ==================================================
        # 게임 진행 중
        # ==================================================

        if game:

            await interaction.response.send_message(
                "🎮 **현재 진행 중인 게임 메뉴**",
                view=SurvivalGameView(game["id"]),
                ephemeral=True
            )

            return

        # ==================================================
        # 게임 진행 중이 아님
        # ==================================================

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

        print(
            "Main menu error:",
            type(e).__name__,
            str(e)
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                f"🔴 메인 메뉴 오류\n"
                f"`{type(e).__name__}`\n"
                f"{str(e)[:500]}",
                ephemeral=True
            )

        else:

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
    description="1인으로 실제 게임 화면을 테스트합니다."
)
async def game_test(
    interaction
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

        (
            game_row,
            player,
            count,
            alive_count
        ) = await get_test_game_screen(
            game["id"],
            interaction.user.id
        )

        embed = discord.Embed(
            title="💰 MONEY BATTLE ROYALE",
            description=(
                "━━━━━━━━━━━━━━━━━━\n"
                "🔥 **SURVIVAL GAME**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👥 생존자: **{alive_count}명**\n"
                "🏆 현재 순위: **1위**\n"
                f"🪙 보유 코인: **{player['money']:,}**\n"
                f"😇 선행 포인트: **{player['good_deed']:,}**\n\n"
                "☠️ 다음 탈락 판정까지 준비 중...\n\n"
                "게임에서 돈을 벌고\n"
                "최후의 1인이 되어보세요!"
            )
        )

        embed.set_footer(
            text=(
                f"Game ID: {game_row['id']} "
                "• TEST MODE"
            )
        )

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=SurvivalGameView(
                game_row["id"]
            )
        )

        print(
            "[GAME TEST] "
            f"game_id={game_row['id']} "
            f"user_id={interaction.user.id}"
        )

    except Exception as e:

        print(
            "Game test error:",
            type(e).__name__,
            str(e)
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                (
                    "🔴 게임 테스트 실패\n"
                    f"`{type(e).__name__}`\n"
                    f"{str(e)[:500]}"
                ),
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                (
                    "🔴 게임 테스트 실패\n"
                    f"`{type(e).__name__}`\n"
                    f"{str(e)[:500]}"
                ),
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
    interaction
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

        print(
            "DB test error:",
            type(e).__name__,
            str(e)
        )

        await interaction.response.send_message(
            (
                "🔴 DB 연결 실패\n"
                f"`{type(e).__name__}`\n"
                f"{str(e)[:500]}"
            ),
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
            "Slash command sync failed:",
            type(e).__name__,
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
            "Database connection failed:",
            type(e).__name__,
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