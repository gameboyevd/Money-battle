import os
import asyncio
import threading
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import json

def parse_game_data(game_data):
    """game_data가 str이든 dict이든 안전하게 dict로 변환"""
    if game_data is None:
        return {}
    if isinstance(game_data, dict):
        return game_data
    if isinstance(game_data, str):
        try:
            return json.loads(game_data)
        except Exception:
            return {}
    return {}

# ============================================================
# 설정
# ============================================================

MIN_PLAYERS = 4
STARTING_MONEY = 10_000
JOB_COOLDOWN_SECONDS = 5 * 60
DEFAULT_ELIMINATION_INTERVAL = 5 * 60  # 5분 (초 단위)

job_cooldowns = {}
_user_locks = defaultdict(asyncio.Lock)
active_elimination_tasks = {}  # game_id: asyncio.Task


def action_lock(user_id: int):
    return _user_locks[user_id]


# ============================================================
# 상점 / 아이템 정의
# ============================================================

# 일반 상점 아이템 (코인 구매, 게임 중)
SHOP_ITEMS = {
    "정찰권": {
        "emoji": "👁️",
        "price": 300_000,
        "desc": "상대의 특정 정보를 확인합니다. (게임 시작 전 10초 사용)",
        "type": "consumable"
    },
    "빨대 쪼옵": {
        "emoji": "🪣",
        "price": 600_000,
        "desc": "원하는 상대의 돈을 랜덤 소액 가져옵니다.",
        "type": "consumable"
    },
    "이벤트 참가권": {
        "emoji": "🎟️",
        "price": 500_000,
        "desc": "보물찾기 등 이벤트에 참가할 수 있습니다.",
        "type": "consumable"
    },
    "시간 연장권": {
        "emoji": "⏳",
        "price": 1_000_000,
        "desc": "다음 탈락 판정을 3분 연장합니다. (같은 구간 중복 불가)",
        "type": "consumable"
    },
    "랜덤박스": {
        "emoji": "🎁",
        "price": 400_000,
        "desc": "개봉 시 코인/다이아/아이템/꽝이 나옵니다. (0~3배)",
        "type": "consumable"
    },
    "밑장빼기권": {
        "emoji": "🎭",
        "price": 800_000,
        "desc": "게임 시작 전 패 하나를 랜덤으로 교체합니다.",
        "type": "consumable"
    },
    "경매 주최권": {
        "emoji": "🔨",
        "price": 1_500_000,
        "desc": "구매 즉시 미스터리 코인 상자 경매를 10초 후 시작합니다.",
        "type": "consumable"
    },
}

# 다이아 상점 (게임 시작 전 전용, /다이아상점)
DIAMOND_SHOP_ITEMS = {
    "시작자금_1만": {
        "emoji": "🪙",
        "price": 5,
        "bonus_money": 10_000,
        "desc": "시작 자금 +10,000 코인"
    },
    "시작자금_5만": {
        "emoji": "🪙",
        "price": 20,
        "bonus_money": 50_000,
        "desc": "시작 자금 +50,000 코인"
    },
    "시작자금_10만": {
        "emoji": "🪙",
        "price": 35,
        "bonus_money": 100_000,
        "desc": "시작 자금 +100,000 코인"
    },
    "시작자금_30만": {
        "emoji": "🪙",
        "price": 90,
        "bonus_money": 300_000,
        "desc": "시작 자금 +300,000 코인"
    },
    "시작자금_80만": {
        "emoji": "🪙",
        "price": 200,
        "bonus_money": 800_000,
        "desc": "시작 자금 +800,000 코인"
    },
}

# 천사의 상점 (게임 중 전용, 선행 포인트 사용, 한 게임당 1개만 구매 가능)
ANGEL_SHOP_ITEMS = {
    "천사의 구원": {
        "emoji": "🪽",
        "price": 3_000_000,
        "desc": "탈락 대상이 되었을 때 1회 생존합니다. (게임당 1회)",
        "effect": "survive_elimination"
    },
    "큐피트 소환권": {
        "emoji": "🏹",
        "price": 6_000_000,
        "desc": "꼴등 포함 랜덤 생존자와 자신을 강제 연결. 꼴등 탈락 시 연결자도 탈락.",
        "effect": "cupid_link"
    },
    "신의 모래시계": {
        "emoji": "⏳",
        "price": 7_000_000,
        "desc": "방금 전 잃은 돈을 1회 복구합니다. (가장 최근 손실만)",
        "effect": "recover_last_loss"
    },
    "천사의 구제": {
        "emoji": "🕊️",
        "price": 8_000_000,
        "desc": "1~3등의 돈 30%를 4등 이하에게 분배합니다. (소수 인원 시 규칙 변경)",
        "effect": "angel_relief"
    },
    "승천궁": {
        "emoji": "🏛️",
        "price": 10_000_000,
        "desc": "탈락자 1명 임시 부활 + 다음 판정 강제 탈락 + 지정 대상 코인 차감 등 최고급 효과",
        "effect": "ascension_palace"
    },
}


# ============================================================
# Render HTTP 서버
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Money Battle Royale Bot is running!")

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"HTTP server started on port {port}")
    server.serve_forever()


# ============================================================
# Discord Bot
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# DB
# ============================================================

async def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL이 설정되지 않았습니다.")
    return await asyncpg.connect(database_url)


async def init_db():
    """필요한 컬럼이 없으면 추가 (idempotent)"""
    connection = await get_db()
    try:
        await connection.execute("""
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS inventory JSONB DEFAULT '{}'::jsonb;
        """)
        await connection.execute("""
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS bonus_starting_money INTEGER DEFAULT 0;
        """)
        # points 컬럼이 없을 수도 있으니 안전하게
        await connection.execute("""
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0;
        """)
        # 천사의 상점 관련
        await connection.execute("""
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS angel_item TEXT DEFAULT NULL;
        """)
        await connection.execute("""
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS last_money_loss BIGINT DEFAULT 0;
        """)
        await connection.execute("""
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS cupid_link_user_id TEXT DEFAULT NULL;
        """)
        print("DB schema check completed (inventory, bonus_starting_money, angel fields)")
    except Exception as e:
        print(f"DB init warning: {type(e).__name__}: {e}")
    finally:
        await connection.close()


# ============================================================
# 플레이어
# ============================================================

async def get_or_create_player(user: discord.User, guild: discord.Guild):
    if guild is None:
        raise RuntimeError("디스코드 서버에서만 사용할 수 있습니다.")

    connection = await get_db()
    try:
        return await connection.fetchrow(
            """
            INSERT INTO players (
                server_id, user_id, discord_id, username,
                money, diamonds, points, good_deed, alive, eliminated,
                inventory, bonus_starting_money, angel_item, last_money_loss, cupid_link_user_id
            )
            VALUES ($1, $2, $2, $3, $4, 0, 0, 0, TRUE, FALSE, '{}'::jsonb, 0, NULL, 0, NULL)
            ON CONFLICT (server_id, discord_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                updated_at = NOW()
            RETURNING *
            """,
            str(guild.id), str(user.id), user.name, STARTING_MONEY
        )
    finally:
        await connection.close()


async def get_player(user_id: str, server_id: str = None):
    connection = await get_db()
    try:
        if server_id:
            return await connection.fetchrow(
                "SELECT * FROM players WHERE user_id = $1 AND server_id = $2",
                str(user_id), str(server_id)
            )
        return await connection.fetchrow(
            "SELECT * FROM players WHERE user_id = $1 LIMIT 1",
            str(user_id)
        )
    finally:
        await connection.close()


def parse_inventory(inv):
    """inventory 컬럼을 dict로 안전하게 변환"""
    if inv is None:
        return {}
    if isinstance(inv, dict):
        return inv
    if isinstance(inv, str):
        try:
            return json.loads(inv)
        except Exception:
            return {}
    return {}


async def get_player_inventory(user_id: str, server_id: str):
    player = await get_player(user_id, server_id)
    if not player:
        return {}
    return parse_inventory(player.get("inventory"))


async def add_item_to_inventory(user_id: str, server_id: str, item_name: str, amount: int = 1):
    """아이템 추가. 성공 시 새 inventory dict 반환"""
    connection = await get_db()
    try:
        player = await connection.fetchrow(
            "SELECT inventory FROM players WHERE user_id = $1 AND server_id = $2",
            str(user_id), str(server_id)
        )
        if not player:
            return None
        inv = parse_inventory(player["inventory"])
        inv[item_name] = inv.get(item_name, 0) + amount
        await connection.execute(
            """
            UPDATE players
            SET inventory = $1::jsonb, updated_at = NOW()
            WHERE user_id = $2 AND server_id = $3
            """,
            json.dumps(inv), str(user_id), str(server_id)
        )
        return inv
    finally:
        await connection.close()


async def remove_item_from_inventory(user_id: str, server_id: str, item_name: str, amount: int = 1):
    """아이템 제거. 성공 시 True"""
    connection = await get_db()
    try:
        player = await connection.fetchrow(
            "SELECT inventory FROM players WHERE user_id = $1 AND server_id = $2",
            str(user_id), str(server_id)
        )
        if not player:
            return False
        inv = parse_inventory(player["inventory"])
        current = inv.get(item_name, 0)
        if current < amount:
            return False
        inv[item_name] = current - amount
        if inv[item_name] <= 0:
            del inv[item_name]
        await connection.execute(
            """
            UPDATE players
            SET inventory = $1::jsonb, updated_at = NOW()
            WHERE user_id = $2 AND server_id = $3
            """,
            json.dumps(inv), str(user_id), str(server_id)
        )
        return True
    finally:
        await connection.close()


async def add_bonus_starting_money(user_id: str, server_id: str, amount: int):
    """다이아 상점에서 시작 자금 보너스 추가"""
    connection = await get_db()
    try:
        await connection.execute(
            """
            UPDATE players
            SET bonus_starting_money = COALESCE(bonus_starting_money, 0) + $1,
                updated_at = NOW()
            WHERE user_id = $2 AND server_id = $3
            """,
            amount, str(user_id), str(server_id)
        )
    finally:
        await connection.close()


# ============================================================
# 게임 관련 함수
# ============================================================

async def get_waiting_game(channel_id: int):
    connection = await get_db()
    try:
        return await connection.fetchrow(
            """
            SELECT * FROM games
            WHERE channel_id = $1 AND status = 'waiting'
            ORDER BY id DESC LIMIT 1
            """,
            str(channel_id)
        )
    finally:
        await connection.close()


async def get_playing_game(channel_id: int):
    connection = await get_db()
    try:
        return await connection.fetchrow(
            """
            SELECT * FROM games
            WHERE channel_id = $1 AND status = 'playing'
            ORDER BY id DESC LIMIT 1
            """,
            str(channel_id)
        )
    finally:
        await connection.close()


async def get_game_by_id(game_id: int):
    connection = await get_db()
    try:
        return await connection.fetchrow("SELECT * FROM games WHERE id = $1", game_id)
    finally:
        await connection.close()


async def create_waiting_game(guild, channel, host_id):
    connection = await get_db()
    try:
        return await connection.fetchrow(
            """
            INSERT INTO games (
                game_type, status, host_id, channel_id,
                current_phase, game_data
            )
            VALUES (
                'money_battle_royale', 'waiting', $1, $2, 'waiting',
                $3::jsonb
            )
            RETURNING *
            """,
            str(host_id), str(channel.id),
            '{"starting_money":10000,"min_players":4,"elimination_interval":300}'
        )
    finally:
        await connection.close()


async def get_or_create_waiting_game(guild, channel, user_id):
    game = await get_waiting_game(channel.id)
    if game:
        return game
    return await create_waiting_game(guild, channel, user_id)


async def get_player_count(game_id):
    connection = await get_db()
    try:
        return await connection.fetchval(
            "SELECT COUNT(*) FROM game_players WHERE game_id = $1", game_id
        )
    finally:
        await connection.close()


async def get_alive_count(game_id):
    connection = await get_db()
    try:
        return await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM game_players gp
            JOIN players p ON p.user_id = gp.user_id
            WHERE gp.game_id = $1 AND p.alive = TRUE AND p.eliminated = FALSE
            """,
            game_id
        )
    finally:
        await connection.close()


async def get_game_players(game_id):
    connection = await get_db()
    try:
        return await connection.fetch(
            """
            SELECT gp.*, p.money, p.alive, p.eliminated, p.username
            FROM game_players gp
            JOIN players p ON p.user_id = gp.user_id
            WHERE gp.game_id = $1
            ORDER BY p.money DESC
            """,
            game_id
        )
    finally:
        await connection.close()


async def get_player_rank(game_id, user_id):
    players = await get_game_players(game_id)
    alive_players = [p for p in players if p["alive"] and not p["eliminated"]]
    for i, p in enumerate(alive_players, 1):
        if str(p["user_id"]) == str(user_id):
            return i, len(alive_players)
    return None, len(alive_players)


# ============================================================
# 참가 / 취소
# ============================================================

async def join_game_player(user, guild, channel):
    await get_or_create_player(user, guild)
    connection = await get_db()
    try:
        async with connection.transaction():
            game = await connection.fetchrow(
                """
                SELECT * FROM games
                WHERE channel_id = $1 AND status = 'waiting'
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                str(channel.id)
            )

            if not game:
                game = await connection.fetchrow(
                    """
                    INSERT INTO games (
                        game_type, status, host_id, channel_id,
                        current_phase, game_data
                    )
                    VALUES (
                        'money_battle_royale', 'waiting', $1, $2, 'waiting',
                        $3::jsonb
                    )
                    RETURNING *
                    """,
                    str(user.id), str(channel.id),
                    '{"starting_money":10000,"min_players":4,"elimination_interval":300}'
                )

            existing = await connection.fetchrow(
                "SELECT * FROM game_players WHERE game_id = $1 AND user_id = $2",
                game["id"], str(user.id)
            )

            if existing:
                count = await connection.fetchval(
                    "SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"]
                )
                return game, False, count

            await connection.execute(
                """
                INSERT INTO game_players (game_id, user_id, bet_amount, result, profit)
                VALUES ($1, $2, 0, NULL, 0)
                ON CONFLICT (game_id, user_id) DO NOTHING
                """,
                game["id"], str(user.id)
            )

            count = await connection.fetchval(
                "SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"]
            )
            return game, True, count
    finally:
        await connection.close()


async def cancel_game_player(user, guild, channel):
    connection = await get_db()
    try:
        async with connection.transaction():
            game = await connection.fetchrow(
                """
                SELECT * FROM games
                WHERE channel_id = $1 AND status = 'waiting'
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                str(channel.id)
            )

            if not game:
                return None, False, 0

            existing = await connection.fetchrow(
                "SELECT * FROM game_players WHERE game_id = $1 AND user_id = $2",
                game["id"], str(user.id)
            )

            if not existing:
                count = await connection.fetchval(
                    "SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"]
                )
                return game, False, count

            await connection.execute(
                "DELETE FROM game_players WHERE game_id = $1 AND user_id = $2",
                game["id"], str(user.id)
            )

            count = await connection.fetchval(
                "SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"]
            )
            return game, True, count
    finally:
        await connection.close()


# ============================================================
# 게임 시작
# ============================================================

async def start_survival_game(game_id):
    connection = await get_db()
    try:
        async with connection.transaction():
            game = await connection.fetchrow(
                "SELECT * FROM games WHERE id = $1 FOR UPDATE", game_id
            )

            if not game:
                raise RuntimeError("게임을 찾을 수 없습니다.")
            if game["status"] != "waiting":
                raise RuntimeError("이미 시작되었거나 종료된 게임입니다.")

            count = await connection.fetchval(
                "SELECT COUNT(*) FROM game_players WHERE game_id = $1", game_id
            )
            if count < MIN_PLAYERS:
                raise RuntimeError(f"최소 {MIN_PLAYERS}명이 필요합니다.")

            # 다음 탈락 시간 설정
            next_elim = datetime.utcnow() + timedelta(seconds=DEFAULT_ELIMINATION_INTERVAL)

            await connection.execute(
                """
                UPDATE games
                SET
                    status = 'playing',
                    current_phase = 'survival',
                    started_at = NOW(),
                    game_data = jsonb_set(
                        COALESCE(game_data, '{}'::jsonb),
                        '{next_elimination_at}',
                        to_jsonb($2::text),
                        TRUE
                    )
                WHERE id = $1 AND status = 'waiting'
                """,
                game_id, next_elim.isoformat()
            )

            players = await connection.fetch(
                "SELECT user_id FROM game_players WHERE game_id = $1", game_id
            )

            for player in players:
                # 다이아 상점에서 구매한 시작 자금 보너스 적용 후 초기화 + 천사 아이템 초기화
                await connection.execute(
                    """
                    UPDATE players
                    SET money = $1 + COALESCE(bonus_starting_money, 0),
                        bonus_starting_money = 0,
                        alive = TRUE,
                        eliminated = FALSE,
                        good_deed = 0,
                        inventory = '{}'::jsonb,
                        angel_item = NULL,
                        last_money_loss = 0,
                        cupid_link_user_id = NULL,
                        updated_at = NOW()
                    WHERE user_id = $2
                    """,
                    STARTING_MONEY, str(player["user_id"])
                )

            return count
    finally:
        await connection.close()


# ============================================================
# 탈락 시스템
# ============================================================

async def eliminate_lowest_players(game_id: int, channel: discord.TextChannel, count: int = 1):
    connection = await get_db()
    try:
        async with connection.transaction():
            # 생존자 중 코인 낮은 순으로 조회 (angel_item, cupid_link 포함)
            alive = await connection.fetch(
                """
                SELECT p.user_id, p.money, p.username, p.angel_item, p.cupid_link_user_id
                FROM game_players gp
                JOIN players p ON p.user_id = gp.user_id
                WHERE gp.game_id = $1 AND p.alive = TRUE AND p.eliminated = FALSE
                ORDER BY p.money ASC, RANDOM()
                """,
                game_id
            )

            if len(alive) <= 1:
                return None  # 이미 1명 이하

            to_eliminate = list(alive[:count])
            eliminated_list = []
            saved_by_angel = []

            # 1차: 천사의 구원 체크
            final_targets = []
            for player in to_eliminate:
                if player["angel_item"] == "천사의 구원":
                    # 구원 발동 → 탈락 면제 + angel_item 소모
                    await connection.execute(
                        """
                        UPDATE players
                        SET angel_item = NULL, updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        str(player["user_id"])
                    )
                    saved_by_angel.append(player)
                else:
                    final_targets.append(player)

            # 2차: 실제 탈락 처리 + 큐피트 연쇄
            extra_from_cupid = []
            for player in final_targets:
                # 탈락 전에 이 사람의 cupid_link_user_id 확인 (꼴등이 탈락할 때 연결된 사람)
                linked_id = player.get("cupid_link_user_id")

                await connection.execute(
                    """
                    UPDATE players
                    SET alive = FALSE, eliminated = TRUE, cupid_link_user_id = NULL, updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    str(player["user_id"])
                )
                eliminated_list.append(player)

                # 큐피트: 꼴등이 탈락하면 연결된 사람도 탈락
                if linked_id:
                    linked_player = await connection.fetchrow(
                        """
                        SELECT user_id, username, money
                        FROM players
                        WHERE user_id = $1
                          AND alive = TRUE AND eliminated = FALSE
                        """,
                        str(linked_id)
                    )
                    if linked_player:
                        await connection.execute(
                            """
                            UPDATE players
                            SET alive = FALSE, eliminated = TRUE, cupid_link_user_id = NULL, updated_at = NOW()
                            WHERE user_id = $1
                            """,
                            str(linked_id)
                        )
                        extra_from_cupid.append(linked_player)
                        eliminated_list.append(linked_player)

            # 구원 알림은 호출 측에서 처리하기 위해 반환
            return {
                "eliminated": eliminated_list,
                "saved": saved_by_angel,
                "cupid_extra": extra_from_cupid
            }
    finally:
        await connection.close()


async def check_and_end_game(game_id: int, channel: discord.TextChannel):
    game = await get_game_by_id(game_id)
    if not game:
        return False

    # 테스트 게임은 강제종료 전까지 계속 진행
    if game["game_type"] == "money_battle_royale_test":
        return False

    alive_count = await get_alive_count(game_id)

    if alive_count > 1:
        return False

    # 우승자 처리
    connection = await get_db()
    try:
        winner = await connection.fetchrow(
            """
            SELECT p.user_id, p.username, p.money
            FROM game_players gp
            JOIN players p ON p.user_id = gp.user_id
            WHERE gp.game_id = $1 AND p.alive = TRUE AND p.eliminated = FALSE
            LIMIT 1
            """,
            game_id
        )

        await connection.execute(
            """
            UPDATE games
            SET status = 'finished', current_phase = 'ended', ended_at = NOW()
            WHERE id = $1
            """,
            game_id
        )

        if winner:
            await connection.execute(
                """
                UPDATE players
                SET diamonds = diamonds + 10, updated_at = NOW()
                WHERE user_id = $1
                """,
                str(winner["user_id"])
            )

            embed = discord.Embed(
                title="👑 머니 배틀로얄 종료!",
                description=(
                    f"🎉 **우승자**: <@{winner['user_id']}>\n"
                    f"🪙 최종 보유 코인: **{winner['money']:,}**\n\n"
                    f"💎 다이아 **+10** 지급!"
                ),
                color=discord.Color.gold()
            )
            await channel.send(embed=embed)

        return True
    finally:
        await connection.close()


async def elimination_loop(game_id: int, channel: discord.TextChannel):
    """탈락 타이머 루프"""
    try:
        while True:
            game = await get_game_by_id(game_id)
            if not game or game["status"] != "playing":
                break

            # 다음 탈락 시간 확인
            game_data = parse_game_data(game["game_data"])
            next_elim_str = game_data.get("next_elimination_at")
            if not next_elim_str:
                break

            next_elim = datetime.fromisoformat(next_elim_str)
            now = datetime.utcnow()

            remaining = (next_elim - now).total_seconds()

            if remaining > 60:
                await asyncio.sleep(min(30, remaining - 60))
                continue
            elif remaining > 30:
                await channel.send("⚠️ **탈락 판정까지 60초!**")
                await asyncio.sleep(remaining - 30)
            elif remaining > 10:
                await channel.send("🚨 **탈락 판정까지 30초!**")
                await asyncio.sleep(remaining - 10)
            elif remaining > 0:
                msg = await channel.send("🔥 **10...**")
                for i in range(9, 0, -1):
                    await asyncio.sleep(1)
                    await msg.edit(content=f"🔥 **{i}...**")
                await asyncio.sleep(1)
            else:
                # 탈락 실행
                result = await eliminate_lowest_players(game_id, channel, count=1)

                if result:
                    # 천사의 구원으로 살아난 사람
                    for p in result.get("saved", []):
                        embed = discord.Embed(
                            title="🪽 천사의 구원 발동!",
                            description=(
                                f"🪽 <@{p['user_id']}> 님이 **천사의 구원**으로 탈락을 면했습니다!\n"
                                f"보유 코인: **{p['money']:,}**"
                            ),
                            color=discord.Color.purple()
                        )
                        await channel.send(embed=embed)

                    # 실제 탈락자
                    for p in result.get("eliminated", []):
                        is_cupid = any(str(c["user_id"]) == str(p["user_id"]) for c in result.get("cupid_extra", []))
                        title = "🏹 큐피트 연쇄 탈락!" if is_cupid else "☠️ 탈락 판정!"
                        embed = discord.Embed(
                            title=title,
                            description=(
                                f"☠️ <@{p['user_id']}> 님이 탈락했습니다.\n"
                                f"최종 보유 코인: **{p['money']:,}**"
                            ),
                            color=discord.Color.red()
                        )
                        await channel.send(embed=embed)

                # 게임 종료 체크
                ended = await check_and_end_game(game_id, channel)
                if ended:
                    break

                # 다음 탈락 시간 갱신
                connection = await get_db()
                try:
                    new_next = datetime.utcnow() + timedelta(seconds=DEFAULT_ELIMINATION_INTERVAL)
                    await connection.execute(
                        """
                        UPDATE games
                        SET game_data = jsonb_set(
                            COALESCE(game_data, '{}'::jsonb),
                            '{next_elimination_at}',
                            to_jsonb($2::text),
                            TRUE
                        )
                        WHERE id = $1
                        """,
                        game_id, new_next.isoformat()
                    )
                finally:
                    await connection.close()

                await asyncio.sleep(2)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Elimination loop error: {type(e).__name__}: {e}")

def start_elimination_task(game_id: int, channel: discord.TextChannel):
    if game_id in active_elimination_tasks:
        active_elimination_tasks[game_id].cancel()

    task = asyncio.create_task(elimination_loop(game_id, channel))
    active_elimination_tasks[game_id] = task


# ============================================================
# 대기방 View
# ============================================================

class WaitingView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=300)
        self.game_id = game_id

    async def interaction_check(self, interaction):
        try:
            game = await get_waiting_game(interaction.channel.id)
            if not game or game["id"] != self.game_id:
                await interaction.response.send_message(
                    "🔒 현재 대기 중인 게임이 없습니다.", ephemeral=True
                )
                return False
            return True
        except Exception as e:
            print("WaitingView check error:", type(e).__name__, str(e))
            await interaction.response.send_message(
                "🔴 게임 상태를 확인할 수 없습니다.", ephemeral=True
            )
            return False

    @discord.ui.button(label="게임 참가", emoji="🎮", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            try:
                game, joined, count = await join_game_player(
                    interaction.user, interaction.guild, interaction.channel
                )
                if not joined:
                    await interaction.response.send_message(
                        f"⚠️ 이미 참가 중입니다.\n👥 현재 참가자: **{count}명**",
                        ephemeral=True
                    )
                    return

                await interaction.response.send_message(
                    f"🎉 게임 참가 완료!\n👥 현재 참가자: **{count}명**\n🎯 최소 인원: **{MIN_PLAYERS}명**",
                    ephemeral=True
                )
            except Exception as e:
                print("Join error:", type(e).__name__, str(e))
                await interaction.response.send_message(
                    f"🔴 참가 실패\n`{type(e).__name__}`", ephemeral=True
                )

    @discord.ui.button(label="참가 취소", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            try:
                game, cancelled, count = await cancel_game_player(
                    interaction.user, interaction.guild, interaction.channel
                )
                if not cancelled:
                    await interaction.response.send_message(
                        f"⚠️ 참가 중이 아닙니다.\n👥 현재 참가자: **{count}명**",
                        ephemeral=True
                    )
                    return

                await interaction.response.send_message(
                    f"❌ 참가를 취소했습니다.\n👥 현재 참가자: **{count}명**",
                    ephemeral=True
                )
            except Exception as e:
                print("Cancel error:", type(e).__name__, str(e))
                await interaction.response.send_message(
                    f"🔴 참가 취소 오류\n`{type(e).__name__}`", ephemeral=True
                )

    @discord.ui.button(label="게임 시작", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            try:
                game = await get_waiting_game(interaction.channel.id)
                if not game or game["id"] != self.game_id:
                    await interaction.response.send_message("⚠️ 대기 중인 게임이 없습니다.", ephemeral=True)
                    return

                if str(game["host_id"]) != str(interaction.user.id):
                    await interaction.response.send_message("🔒 방장만 시작할 수 있습니다.", ephemeral=True)
                    return

                count = await get_player_count(game["id"])
                if count < MIN_PLAYERS:
                    await interaction.response.send_message(
                        f"⚠️ 최소 {MIN_PLAYERS}명이 필요합니다.\n현재: **{count}명**",
                        ephemeral=True
                    )
                    return

                await interaction.response.send_message("🎮 게임 시작 준비 중...\n⏳ **3초 후 시작!**")

                for number in [3, 2, 1]:
                    await asyncio.sleep(1)
                    await interaction.edit_original_response(
                        content=f"🎮 **MONEY BATTLE ROYALE**\n\n🔥 시작까지 **{number}초**!"
                    )

                started_count = await start_survival_game(game["id"])

                # 탈락 타이머 시작
                start_elimination_task(game["id"], interaction.channel)

                # 메인 UI 전송
                view = SurvivalGameView(game["id"])
                embed = await build_main_embed(game["id"], interaction.user.id, interaction.guild.id)

                await interaction.edit_original_response(
                    content=None,
                    embed=embed,
                    view=view
                )

            except Exception as e:
                print("Start error:", type(e).__name__, str(e))
                if interaction.response.is_done():
                    await interaction.followup.send(
                        f"🔴 게임 시작 실패\n`{type(e).__name__}`: {str(e)[:300]}",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        f"🔴 게임 시작 실패\n`{type(e).__name__}`", ephemeral=True
                    )


# ============================================================
# 메인 Embed 생성
# ============================================================

async def build_main_embed(game_id: int, user_id: int, guild_id: int):
    player = await get_player(user_id, guild_id)
    rank, alive_count = await get_player_rank(game_id, user_id)
    game = await get_game_by_id(game_id)

    next_elim_text = "계산 중..."
    if game:
        game_data = parse_game_data(game["game_data"])
        next_str = game_data.get("next_elimination_at")
        if next_str:
            try:
                next_time = datetime.fromisoformat(next_str)
                remaining = max(0, int((next_time - datetime.utcnow()).total_seconds()))
                minutes = remaining // 60
                seconds = remaining % 60
                next_elim_text = f"{minutes:02d}:{seconds:02d}"
            except Exception:
                next_elim_text = "오류"

    money = player["money"] if player else 0
    diamonds = player["diamonds"] if player else 0
    good_deed = player["good_deed"] if player else 0
    is_alive = player["alive"] if player else False
    bonus = player.get("bonus_starting_money") or 0 if player else 0
    inv = parse_inventory(player.get("inventory") if player else None)
    angel_item = player.get("angel_item") if player else None

    status = "🟢 생존 중" if is_alive else "☠️ 탈락"

    inv_text = "없음"
    if inv:
        parts = []
        for name, cnt in inv.items():
            emoji = SHOP_ITEMS.get(name, {}).get("emoji", "📦")
            parts.append(f"{emoji}{name}×{cnt}")
        inv_text = " ".join(parts) if parts else "없음"

    angel_text = "없음"
    if angel_item:
        emoji = ANGEL_SHOP_ITEMS.get(angel_item, {}).get("emoji", "🪽")
        angel_text = f"{emoji} {angel_item}"

    embed = discord.Embed(
        title="💰 MONEY BATTLE ROYALE",
        color=discord.Color.gold() if is_alive else discord.Color.dark_grey()
    )
    embed.add_field(name="🪙 코인", value=f"**{money:,}**", inline=True)
    embed.add_field(name="💎 다이아", value=f"**{diamonds}**", inline=True)
    embed.add_field(name="😇 선행", value=f"**{good_deed:,}**", inline=True)
    embed.add_field(name="👥 생존자", value=f"**{alive_count}명**", inline=True)
    embed.add_field(name="📊 순위", value=f"**{rank}위**" if rank else "탈락", inline=True)
    embed.add_field(name="⏰ 다음 탈락", value=f"**{next_elim_text}**", inline=True)
    if bonus > 0:
        embed.add_field(name="🪙 시작 보너스(미적용)", value=f"**+{bonus:,}**", inline=False)
    embed.add_field(name="🎒 보유 아이템", value=inv_text, inline=False)
    embed.add_field(name="🪽 천사 아이템", value=angel_text, inline=False)
    embed.add_field(name="상태", value=status, inline=False)
    embed.set_footer(text="버튼을 눌러 행동을 선택하세요")

    return embed


# ============================================================
# 알바 관련 (미니게임화)
# ============================================================

JOBS = {
    "청소": {"emoji": "🧹", "reward": 20_000, "base": 12_000},
    "택배": {"emoji": "📦", "reward": 22_000, "base": 14_000},
    "과녁": {"emoji": "🎯", "reward": 22_000, "base": 14_000},
    "패스트푸드": {"emoji": "🍔", "reward": 25_000, "base": 16_000},
    "배달": {"emoji": "🏃", "reward": 25_000, "base": 16_000},
    "주방": {"emoji": "🍳", "reward": 28_000, "base": 18_000},
    "데이터 입력": {"emoji": "🧠", "reward": 30_000, "base": 20_000},
    "낚시": {"emoji": "🎣", "reward": 30_000, "base": 20_000},
}


def get_job_remaining(user_id: int):
    cooldown = job_cooldowns.get(user_id)
    if cooldown is None:
        return 0
    now = datetime.utcnow()
    if now >= cooldown:
        job_cooldowns.pop(user_id, None)
        return 0
    return int((cooldown - now).total_seconds())


def format_seconds(seconds: int):
    minutes = seconds // 60
    seconds %= 60
    if minutes > 0:
        return f"{minutes}분 {seconds}초"
    return f"{seconds}초"


async def give_job_reward(user: discord.User, guild: discord.Guild, amount: int, job_name: str):
    """알바 보상 지급 공통 함수"""
    connection = await get_db()
    try:
        new_money = await connection.fetchval(
            """
            UPDATE players
            SET money = money + $1, updated_at = NOW()
            WHERE server_id = $2 AND user_id = $3
            RETURNING money
            """,
            amount, str(guild.id), str(user.id)
        )
        return new_money
    finally:
        await connection.close()


# ---------- 알바 미니게임 Views ----------

class CleaningMiniGame(discord.ui.View):
    """청소: 오염물 버튼을 제한 시간 내 클릭"""
    def __init__(self, game_id, user_id, guild_id):
        super().__init__(timeout=25)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.hits = 0
        self.target = random.randint(4, 6)
        self.finished = False
        # 오염물 + 가짜 버튼 섞기
        spots = ["💩", "🦠", "🗑️", "🧹", "✨", "🪟", "🧽", "🧴"]
        random.shuffle(spots)
        dirty = set(random.sample(spots, self.target))
        for i, emoji in enumerate(spots):
            is_dirty = emoji in dirty
            btn = discord.ui.Button(
                label="오염" if is_dirty else "깨끗",
                emoji=emoji,
                style=discord.ButtonStyle.danger if is_dirty else discord.ButtonStyle.secondary,
                row=i // 4,
                custom_id=f"clean_{i}_{is_dirty}"
            )
            btn.callback = self.make_callback(is_dirty)
            self.add_item(btn)

    def make_callback(self, is_dirty):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("본인 알바만 가능합니다.", ephemeral=True)
                return
            if self.finished:
                await interaction.response.defer()
                return
            if is_dirty:
                self.hits += 1
                await interaction.response.send_message(f"✅ 청소 완료! ({self.hits}/{self.target})", ephemeral=True)
                if self.hits >= self.target:
                    await self.finish(interaction, True)
            else:
                await interaction.response.send_message("❌ 깨끗한 곳을 건드렸습니다!", ephemeral=True)
                await self.finish(interaction, False)
        return callback

    async def finish(self, interaction, success):
        if self.finished:
            return
        self.finished = True
        self.stop()
        job = JOBS["청소"]
        if success:
            bonus = int(job["base"] * (0.8 + self.hits * 0.15))
            reward = min(job["reward"] + 8_000, bonus)
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "청소")
            await interaction.followup.send(
                f"🧹 **청소 알바 성공!**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n⏳ 다음 알바까지 5분",
                ephemeral=True
            )
        else:
            reward = job["base"] // 3
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "청소")
            await interaction.followup.send(
                f"🧹 청소 실패... 기본급만 지급\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**",
                ephemeral=True
            )


class TargetMiniGame(discord.ui.View):
    """과녁: 나타나는 과녁을 빠르게 클릭"""
    def __init__(self, game_id, user_id, guild_id):
        super().__init__(timeout=20)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.hits = 0
        self.needed = 5
        self.finished = False
        self.round = 0
        self._spawn_target()

    def _spawn_target(self):
        self.clear_items()
        positions = list(range(8))
        random.shuffle(positions)
        target_pos = positions[0]
        for i in range(8):
            is_target = (i == target_pos)
            btn = discord.ui.Button(
                label="🎯" if is_target else "·",
                style=discord.ButtonStyle.success if is_target else discord.ButtonStyle.secondary,
                row=i // 4,
                custom_id=f"tgt_{self.round}_{i}"
            )
            btn.callback = self.make_callback(is_target)
            self.add_item(btn)

    def make_callback(self, is_target):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("본인 알바만 가능합니다.", ephemeral=True)
                return
            if self.finished:
                await interaction.response.defer()
                return
            if is_target:
                self.hits += 1
                self.round += 1
                if self.hits >= self.needed:
                    await self.finish(interaction, True)
                else:
                    self._spawn_target()
                    await interaction.response.edit_message(
                        content=f"🎯 과녁 적중! ({self.hits}/{self.needed})\n다음 과녁을 클릭하세요!",
                        view=self
                    )
            else:
                await self.finish(interaction, False)
        return callback

    async def finish(self, interaction, success):
        if self.finished:
            return
        self.finished = True
        self.stop()
        job = JOBS["과녁"]
        if success:
            reward = job["reward"] + self.hits * 1_500
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "과녁")
            msg = f"🎯 **과녁 알바 완벽!**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n⏳ 다음 알바까지 5분"
        else:
            reward = job["base"] // 2 + self.hits * 2_000
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "과녁")
            msg = f"🎯 과녁 종료 (적중 {self.hits}회)\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**"
        try:
            await interaction.response.edit_message(content=msg, view=None)
        except Exception:
            await interaction.followup.send(msg, ephemeral=True)


class FishingMiniGame(discord.ui.View):
    """낚시: 타이밍 맞춰 버튼 누르기"""
    def __init__(self, game_id, user_id, guild_id):
        super().__init__(timeout=15)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.phase = "wait"  # wait -> bite -> done
        self.finished = False
        self.bite_time = None
        btn = discord.ui.Button(label="🎣 낚싯대 당기기!", style=discord.ButtonStyle.primary, emoji="🎣")
        btn.callback = self.pull
        self.add_item(btn)

    async def start_bite(self, interaction):
        await asyncio.sleep(random.uniform(2.5, 6.0))
        if self.finished:
            return
        self.phase = "bite"
        self.bite_time = datetime.utcnow()
        try:
            await interaction.edit_original_response(
                content="🐟 **입질이 왔다!!!** 지금 바로 당겨라!!!",
                view=self
            )
        except Exception:
            pass
        await asyncio.sleep(1.8)
        if self.phase == "bite" and not self.finished:
            self.finished = True
            self.stop()
            reward = JOBS["낚시"]["base"] // 4
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "낚시")
            try:
                await interaction.edit_original_response(
                    content=f"💨 물고기가 도망갔습니다...\n💰 +**{reward:,}** (위로금)\n🪙 현재: **{new_money:,}**",
                    view=None
                )
            except Exception:
                pass

    async def pull(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인 알바만 가능합니다.", ephemeral=True)
            return
        if self.finished:
            await interaction.response.defer()
            return

        if self.phase == "wait":
            await interaction.response.send_message("아직 입질이 없습니다... 기다리세요!", ephemeral=True)
            return

        if self.phase == "bite":
            self.finished = True
            self.stop()
            elapsed = (datetime.utcnow() - self.bite_time).total_seconds()
            job = JOBS["낚시"]
            if elapsed < 0.9:
                reward = job["reward"] + 12_000  # 완벽한 타이밍
                grade = "🏆 대어 낚음!"
            elif elapsed < 1.5:
                reward = job["reward"]
                grade = "✨ 성공!"
            else:
                reward = job["base"]
                grade = "보통 물고기"
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "낚시")
            await interaction.response.edit_message(
                content=f"🎣 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n⏳ 다음 알바까지 5분",
                view=None
            )


class DataInputMiniGame(discord.ui.View):
    """데이터 입력: 잠깐 보여준 문자열을 입력"""
    def __init__(self, game_id, user_id, guild_id):
        super().__init__(timeout=30)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5))
        self.finished = False

    @discord.ui.button(label="입력하기", emoji="⌨️", style=discord.ButtonStyle.primary)
    async def input_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인 알바만 가능합니다.", ephemeral=True)
            return
        modal = DataInputModal(self)
        await interaction.response.send_modal(modal)


class DataInputModal(discord.ui.Modal, title="🧠 데이터 입력"):
    def __init__(self, parent: DataInputMiniGame):
        super().__init__()
        self.parent = parent
        self.answer = discord.ui.TextInput(
            label="방금 본 코드를 입력하세요",
            placeholder="예: A3K9P",
            min_length=3,
            max_length=8,
            required=True
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        if self.parent.finished:
            await interaction.response.send_message("이미 종료된 알바입니다.", ephemeral=True)
            return
        self.parent.finished = True
        self.parent.stop()
        job = JOBS["데이터 입력"]
        if self.answer.value.strip().upper() == self.parent.code:
            reward = job["reward"] + 10_000
            grade = "정확 입력!"
        else:
            reward = job["base"] // 2
            grade = f"오답 (정답: {self.parent.code})"
        job_cooldowns[self.parent.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
        new_money = await give_job_reward(interaction.user, interaction.guild, reward, "데이터 입력")
        await interaction.response.send_message(
            f"🧠 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n⏳ 다음 알바까지 5분",
            ephemeral=True
        )


class FastFoodMiniGame(discord.ui.View):
    """패스트푸드: 주문 순서대로 재료 클릭"""
    def __init__(self, game_id, user_id, guild_id):
        super().__init__(timeout=25)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.order = random.sample(["🍔", "🍟", "🥤", "🍗", "🥗"], k=3)
        self.progress = 0
        self.finished = False
        for i, item in enumerate(["🍔", "🍟", "🥤", "🍗", "🥗"]):
            btn = discord.ui.Button(label=item, style=discord.ButtonStyle.primary, row=0 if i < 3 else 1)
            btn.callback = self.make_callback(item)
            self.add_item(btn)

    def make_callback(self, item):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("본인 알바만 가능합니다.", ephemeral=True)
                return
            if self.finished:
                await interaction.response.defer()
                return
            expected = self.order[self.progress]
            if item == expected:
                self.progress += 1
                if self.progress >= len(self.order):
                    await self.finish(interaction, True)
                else:
                    remain = " → ".join(self.order[self.progress:])
                    await interaction.response.edit_message(
                        content=f"🍔 주문: {' → '.join(self.order)}\n✅ 진행중... 남은 순서: {remain}",
                        view=self
                    )
            else:
                await self.finish(interaction, False)
        return callback

    async def finish(self, interaction, success):
        if self.finished:
            return
        self.finished = True
        self.stop()
        job = JOBS["패스트푸드"]
        if success:
            reward = job["reward"] + 8_000
            grade = "완벽한 주문 처리!"
        else:
            reward = job["base"] // 2
            grade = "주문 실수..."
        job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
        new_money = await give_job_reward(interaction.user, interaction.guild, reward, "패스트푸드")
        try:
            await interaction.response.edit_message(
                content=f"🍔 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n⏳ 다음 알바까지 5분",
                view=None
            )
        except Exception:
            await interaction.followup.send(
                f"🍔 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**",
                ephemeral=True
            )


class SimpleJobMiniGame(discord.ui.View):
    """택배/배달/주방용 간단 성공률 미니게임 (버튼 연타)"""
    def __init__(self, game_id, user_id, guild_id, job_name):
        super().__init__(timeout=18)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.job_name = job_name
        self.clicks = 0
        self.needed = random.randint(6, 9)
        self.finished = False
        btn = discord.ui.Button(
            label=f"{JOBS[job_name]['emoji']} 작업하기!",
            style=discord.ButtonStyle.success
        )
        btn.callback = self.click
        self.add_item(btn)

    async def click(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인 알바만 가능합니다.", ephemeral=True)
            return
        if self.finished:
            await interaction.response.defer()
            return
        self.clicks += 1
        if self.clicks >= self.needed:
            self.finished = True
            self.stop()
            job = JOBS[self.job_name]
            # 클릭 속도 보너스
            reward = job["reward"] + random.randint(0, 6_000)
            job_cooldowns[self.user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, self.job_name)
            await interaction.response.edit_message(
                content=(
                    f"{job['emoji']} **{self.job_name} 알바 완료!**\n"
                    f"💰 +**{reward:,}** 코인\n"
                    f"🪙 현재: **{new_money:,}**\n"
                    f"⏳ 다음 알바까지 5분"
                ),
                view=None
            )
        else:
            await interaction.response.edit_message(
                content=f"{JOBS[self.job_name]['emoji']} 작업 중... ({self.clicks}/{self.needed})",
                view=self
            )


class JobView(discord.ui.View):
    """알바 선택 메뉴 → 미니게임 시작"""
    def __init__(self, game_id):
        super().__init__(timeout=180)
        self.game_id = game_id

    async def interaction_check(self, interaction):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "playing":
            await interaction.response.send_message("🔒 게임이 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    async def start_job(self, interaction, job_name):
        user_id = interaction.user.id
        remaining = get_job_remaining(user_id)
        if remaining > 0:
            await interaction.response.send_message(
                f"⏳ 아직 알바를 할 수 없습니다.\n남은 시간: **{format_seconds(remaining)}**",
                ephemeral=True
            )
            return

        await get_or_create_player(interaction.user, interaction.guild)

        if job_name == "청소":
            view = CleaningMiniGame(self.game_id, user_id, interaction.guild.id)
            await interaction.response.send_message(
                f"🧹 **청소 알바**\n오염된 곳만 클릭하세요! (목표 {view.target}개)\n⏰ 25초 제한",
                view=view, ephemeral=True
            )
        elif job_name == "과녁":
            view = TargetMiniGame(self.game_id, user_id, interaction.guild.id)
            await interaction.response.send_message(
                f"🎯 **과녁 알바**\n과녁(🎯)만 정확히 클릭하세요! (5회)\n⏰ 20초 제한",
                view=view, ephemeral=True
            )
        elif job_name == "낚시":
            view = FishingMiniGame(self.game_id, user_id, interaction.guild.id)
            await interaction.response.send_message(
                "🎣 **낚시 알바**\n입질이 올 때까지 기다린 후 타이밍에 맞춰 당기세요!",
                view=view, ephemeral=True
            )
            asyncio.create_task(view.start_bite(interaction))
        elif job_name == "데이터 입력":
            view = DataInputMiniGame(self.game_id, user_id, interaction.guild.id)
            await interaction.response.send_message(
                f"🧠 **데이터 입력 알바**\n코드를 기억하세요!\n\n# `{view.code}`\n\n"
                f"3초 후 사라집니다. 기억한 뒤 **입력하기**를 누르세요!",
                view=view, ephemeral=True
            )
            await asyncio.sleep(3.5)
            try:
                await interaction.edit_original_response(
                    content="🧠 코드가 사라졌습니다. **입력하기** 버튼을 눌러 입력하세요!",
                    view=view
                )
            except Exception:
                pass
        elif job_name == "패스트푸드":
            view = FastFoodMiniGame(self.game_id, user_id, interaction.guild.id)
            order_str = " → ".join(view.order)
            await interaction.response.send_message(
                f"🍔 **패스트푸드 알바**\n주문 순서대로 재료를 클릭하세요!\n📋 주문: **{order_str}**",
                view=view, ephemeral=True
            )
        else:
            # 택배, 배달, 주방
            view = SimpleJobMiniGame(self.game_id, user_id, interaction.guild.id, job_name)
            await interaction.response.send_message(
                f"{JOBS[job_name]['emoji']} **{job_name} 알바**\n버튼을 연타해서 작업을 완료하세요!",
                view=view, ephemeral=True
            )

    @discord.ui.button(label="청소", emoji="🧹", style=discord.ButtonStyle.primary, row=0)
    async def cleaning(self, interaction, button):
        await self.start_job(interaction, "청소")

    @discord.ui.button(label="택배", emoji="📦", style=discord.ButtonStyle.primary, row=0)
    async def package(self, interaction, button):
        await self.start_job(interaction, "택배")

    @discord.ui.button(label="과녁", emoji="🎯", style=discord.ButtonStyle.primary, row=0)
    async def target(self, interaction, button):
        await self.start_job(interaction, "과녁")

    @discord.ui.button(label="패스트푸드", emoji="🍔", style=discord.ButtonStyle.primary, row=1)
    async def fast_food(self, interaction, button):
        await self.start_job(interaction, "패스트푸드")

    @discord.ui.button(label="배달", emoji="🏃", style=discord.ButtonStyle.primary, row=1)
    async def delivery(self, interaction, button):
        await self.start_job(interaction, "배달")

    @discord.ui.button(label="주방", emoji="🍳", style=discord.ButtonStyle.primary, row=1)
    async def kitchen(self, interaction, button):
        await self.start_job(interaction, "주방")

    @discord.ui.button(label="데이터 입력", emoji="🧠", style=discord.ButtonStyle.primary, row=2)
    async def data_input(self, interaction, button):
        await self.start_job(interaction, "데이터 입력")

    @discord.ui.button(label="낚시", emoji="🎣", style=discord.ButtonStyle.primary, row=2)
    async def fishing(self, interaction, button):
        await self.start_job(interaction, "낚시")

    @discord.ui.button(label="닫기", emoji="❌", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction, button):
        await interaction.response.edit_message(content="알바 메뉴를 닫았습니다.", embed=None, view=None)


# ============================================================
# 일반 상점 View
# ============================================================

class ShopView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=180)
        self.game_id = game_id
        # Select 메뉴 추가
        options = []
        for name, item in SHOP_ITEMS.items():
            options.append(discord.SelectOption(
                label=f"{name} - {item['price']:,}코인",
                value=name,
                description=item["desc"][:50],
                emoji=item["emoji"]
            ))
        self.select = discord.ui.Select(
            placeholder="구매할 아이템을 선택하세요",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "playing":
            await interaction.response.send_message("🔒 게임이 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        item_name = self.select.values[0]
        item = SHOP_ITEMS.get(item_name)
        if not item:
            await interaction.response.send_message("존재하지 않는 아이템입니다.", ephemeral=True)
            return

        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            player = await get_or_create_player(interaction.user, interaction.guild)
            if player["money"] < item["price"]:
                await interaction.response.send_message(
                    f"🔴 코인이 부족합니다.\n필요: **{item['price']:,}** / 보유: **{player['money']:,}**",
                    ephemeral=True
                )
                return

            # 코인 차감 + 아이템 지급
            connection = await get_db()
            try:
                new_money = await connection.fetchval(
                    """
                    UPDATE players
                    SET money = money - $1, updated_at = NOW()
                    WHERE server_id = $2 AND user_id = $3 AND money >= $1
                    RETURNING money
                    """,
                    item["price"], str(interaction.guild.id), str(interaction.user.id)
                )
                if new_money is None:
                    await interaction.response.send_message("🔴 구매 실패 (잔액 부족)", ephemeral=True)
                    return
            finally:
                await connection.close()

            inv = await add_item_to_inventory(
                str(interaction.user.id), str(interaction.guild.id), item_name, 1
            )

            await interaction.response.send_message(
                f"{item['emoji']} **{item_name}** 구매 완료!\n"
                f"💰 -**{item['price']:,}** 코인\n"
                f"🪙 현재: **{new_money:,}** 코인\n"
                f"🎒 보유: **{inv.get(item_name, 0)}개**",
                ephemeral=True
            )


# ============================================================
# 기부 View
# ============================================================

class DonateAmountModal(discord.ui.Modal, title="😇 기부하기"):
    def __init__(self, game_id, target_user_id, target_name):
        super().__init__()
        self.game_id = game_id
        self.target_user_id = target_user_id
        self.target_name = target_name
        self.amount = discord.ui.TextInput(
            label="기부할 코인 금액",
            placeholder="예: 50000",
            min_length=1,
            max_length=12,
            required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount.value.replace(",", "").strip())
            if amount <= 0:
                await interaction.response.send_message("금액은 1 이상이어야 합니다.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("올바른 숫자를 입력해주세요.", ephemeral=True)
            return

        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            # 기부자 확인
            donor = await get_player(str(user_id), str(interaction.guild.id))
            if not donor or not donor["alive"] or donor["eliminated"]:
                await interaction.response.send_message("탈락자는 기부할 수 없습니다.", ephemeral=True)
                return
            if donor["money"] < amount:
                await interaction.response.send_message(
                    f"코인이 부족합니다. 보유: **{donor['money']:,}**", ephemeral=True
                )
                return

            # 대상이 아직 꼴등인지 재확인 (간단히 진행)
            connection = await get_db()
            try:
                async with connection.transaction():
                    # 기부자 차감 + 선행 포인트 증가 (1:1)
                    new_donor_money = await connection.fetchval(
                        """
                        UPDATE players
                        SET money = money - $1,
                            good_deed = COALESCE(good_deed, 0) + $1,
                            updated_at = NOW()
                        WHERE user_id = $2 AND server_id = $3 AND money >= $1
                        RETURNING money
                        """,
                        amount, str(user_id), str(interaction.guild.id)
                    )
                    if new_donor_money is None:
                        await interaction.response.send_message("기부 실패 (잔액 부족)", ephemeral=True)
                        return

                    # 수혜자 증가
                    new_target_money = await connection.fetchval(
                        """
                        UPDATE players
                        SET money = money + $1, updated_at = NOW()
                        WHERE user_id = $2 AND server_id = $3
                        RETURNING money
                        """,
                        amount, str(self.target_user_id), str(interaction.guild.id)
                    )
            finally:
                await connection.close()

            # 공개 알림 (원래 채널)
            embed = discord.Embed(
                title="😇 기부 발생!",
                description=(
                    f"<@{user_id}> 님이 <@{self.target_user_id}> 님에게\n"
                    f"**{amount:,}** 코인을 기부했습니다!\n\n"
                    f"😇 선행 포인트 +**{amount:,}**"
                ),
                color=discord.Color.green()
            )
            await interaction.channel.send(embed=embed)

            await interaction.response.send_message(
                f"😇 기부 완료!\n"
                f"→ <@{self.target_user_id}> 에게 **{amount:,}** 코인\n"
                f"🪙 내 잔액: **{new_donor_money:,}**\n"
                f"😇 선행 포인트 +**{amount:,}**",
                ephemeral=True
            )


class DonateView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=120)
        self.game_id = game_id

    async def interaction_check(self, interaction):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "playing":
            await interaction.response.send_message("🔒 게임이 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="꼴등에게 기부하기", emoji="😇", style=discord.ButtonStyle.success)
    async def donate_to_lowest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 현재 생존자 중 코인 최저 찾기
        players = await get_game_players(self.game_id)
        alive = [p for p in players if p["alive"] and not p["eliminated"]]
        if not alive:
            await interaction.response.send_message("생존자가 없습니다.", ephemeral=True)
            return

        # 자신 제외한 최저
        others = [p for p in alive if str(p["user_id"]) != str(interaction.user.id)]
        if not others:
            await interaction.response.send_message("기부할 대상이 없습니다 (혼자입니다).", ephemeral=True)
            return

        lowest = min(others, key=lambda p: p["money"])
        modal = DonateAmountModal(self.game_id, lowest["user_id"], lowest["username"])
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="닫기", emoji="❌", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="기부 메뉴를 닫았습니다.", embed=None, view=None)


# ============================================================
# 천사의 상점 View
# ============================================================

class AngelShopView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=180)
        self.game_id = game_id
        options = []
        for name, item in ANGEL_SHOP_ITEMS.items():
            options.append(discord.SelectOption(
                label=f"{name} - {item['price']:,}P",
                value=name,
                description=item["desc"][:50],
                emoji=item["emoji"]
            ))
        self.select = discord.ui.Select(
            placeholder="구매할 천사 아이템을 선택하세요 (한 게임당 1개)",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "playing":
            await interaction.response.send_message("🔒 게임이 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        item_name = self.select.values[0]
        item = ANGEL_SHOP_ITEMS.get(item_name)
        if not item:
            await interaction.response.send_message("존재하지 않는 아이템입니다.", ephemeral=True)
            return

        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            player = await get_or_create_player(interaction.user, interaction.guild)

            # 이미 천사 아이템을 구매했는지 확인 (한 게임당 1개)
            if player.get("angel_item"):
                await interaction.response.send_message(
                    f"⚠️ 이미 천사의 상점에서 **{player['angel_item']}** 을(를) 구매했습니다.\n"
                    "한 게임에서는 **단 하나의 아이템만** 구매할 수 있습니다.",
                    ephemeral=True
                )
                return

            if not player["alive"] or player["eliminated"]:
                await interaction.response.send_message("탈락자는 천사의 상점을 이용할 수 없습니다.", ephemeral=True)
                return

            good_deed = player.get("good_deed") or 0
            if good_deed < item["price"]:
                await interaction.response.send_message(
                    f"🔴 선행 포인트가 부족합니다.\n"
                    f"필요: **{item['price']:,}P** / 보유: **{good_deed:,}P**",
                    ephemeral=True
                )
                return

            # 구매 처리
            connection = await get_db()
            try:
                new_good_deed = await connection.fetchval(
                    """
                    UPDATE players
                    SET good_deed = good_deed - $1,
                        angel_item = $2,
                        updated_at = NOW()
                    WHERE server_id = $3 AND user_id = $4
                      AND COALESCE(good_deed, 0) >= $1
                      AND angel_item IS NULL
                    RETURNING good_deed
                    """,
                    item["price"], item_name,
                    str(interaction.guild.id), str(interaction.user.id)
                )
                if new_good_deed is None:
                    await interaction.response.send_message(
                        "구매 실패 (선행 포인트 부족 또는 이미 구매함)", ephemeral=True
                    )
                    return
            finally:
                await connection.close()

            # 효과 즉시 발동 가능한 것들 처리
            effect_msg = ""
            if item["effect"] == "cupid_link":
                effect_msg = await self._activate_cupid(interaction, str(interaction.user.id))
            elif item["effect"] == "angel_relief":
                effect_msg = await self._activate_angel_relief(interaction)
            elif item["effect"] == "recover_last_loss":
                effect_msg = await self._activate_recover_loss(interaction)
            # 천사의 구원 / 승천궁은 나중에 발동 (탈락 시점 등)

            # 공개 알림
            public_embed = discord.Embed(
                title="😇 천사의 상점 구매!",
                description=(
                    f"<@{user_id}> 님이 **{item['emoji']} {item_name}** 을(를) 구매했습니다!\n"
                    f"선행 포인트 -**{item['price']:,}P**"
                ),
                color=discord.Color.purple()
            )
            await interaction.channel.send(embed=public_embed)

            await interaction.response.send_message(
                f"{item['emoji']} **{item_name}** 구매 완료!\n"
                f"😇 -**{item['price']:,}** 선행 포인트\n"
                f"😇 남은 선행 포인트: **{new_good_deed:,}P**\n\n"
                f"{item['desc']}\n"
                f"{effect_msg}",
                ephemeral=True
            )

    async def _activate_cupid(self, interaction, buyer_id: str) -> str:
        """
        큐피트 소환권 (수정됨):
        - 현재 꼴등과 '자기 포함 랜덤한 사람'을 이어줌
        - 꼴등이 탈락하면 연결된 사람도 같이 탈락
        """
        players = await get_game_players(self.game_id)
        alive = [p for p in players if p["alive"] and not p["eliminated"]]
        if len(alive) < 2:
            return "⚠️ 연결할 대상이 부족합니다."

        # 현재 꼴등 찾기
        lowest = min(alive, key=lambda p: p["money"])
        lowest_id = str(lowest["user_id"])

        # 꼴등을 제외한 나머지 중에서 랜덤 선택 (자기 자신 포함 가능)
        candidates = [p for p in alive if str(p["user_id"]) != lowest_id]
        if not candidates:
            return "⚠️ 연결할 대상이 없습니다."

        linked = random.choice(candidates)  # 자기 자신 포함 랜덤
        linked_id = str(linked["user_id"])

        connection = await get_db()
        try:
            # 꼴등 → 연결된 사람 방향으로 저장
            # (꼴등이 탈락할 때 linked_id가 같이 탈락되도록)
            await connection.execute(
                """
                UPDATE players
                SET cupid_link_user_id = $1, updated_at = NOW()
                WHERE user_id = $2
                """,
                linked_id, lowest_id
            )
            # 구매자에게도 표시용으로 기록 (선택)
            await connection.execute(
                """
                UPDATE players
                SET cupid_link_user_id = $1, updated_at = NOW()
                WHERE user_id = $2
                """,
                lowest_id, buyer_id
            )
        finally:
            await connection.close()

        public = discord.Embed(
            title="🏹 큐피트가 소환되었습니다!",
            description=(
                f"**꼴등** <@{lowest_id}> 님과 <@{linked_id}> 님이 **강제 연결**되었습니다!\n\n"
                f"☠️ 꼴등이 탈락하면 연결된 사람도 **함께 탈락**합니다."
            ),
            color=discord.Color.pink()
        )
        await interaction.channel.send(embed=public)
        return f"🏹 꼴등 <@{lowest_id}> ↔ <@{linked_id}> 연결 완료!"

    async def _activate_angel_relief(self, interaction) -> str:
        """천사의 구제: 1~3등 돈의 30%를 4등 이하에게 분배"""
        players = await get_game_players(self.game_id)
        alive = sorted(
            [p for p in players if p["alive"] and not p["eliminated"]],
            key=lambda p: p["money"],
            reverse=True
        )
        n = len(alive)
        if n <= 1:
            return "⚠️ 인원이 부족하여 효과가 발동되지 않았습니다."

        connection = await get_db()
        try:
            async with connection.transaction():
                if n == 2:
                    # 1대1 → 사용 불가
                    return "⚠️ 1대1 상황에서는 천사의 구제를 사용할 수 없습니다."
                elif n == 3:
                    # 1,2등 돈의 15%를 3등에게
                    top = alive[:2]
                    bottom = alive[2:]
                    ratio = 0.15
                else:
                    # 1~3등 30% → 4등 이하
                    top = alive[:3]
                    bottom = alive[3:]
                    ratio = 0.30

                total_pool = 0
                for p in top:
                    take = int(p["money"] * ratio)
                    if take <= 0:
                        continue
                    await connection.execute(
                        "UPDATE players SET money = money - $1, updated_at = NOW() WHERE user_id = $2",
                        take, str(p["user_id"])
                    )
                    total_pool += take

                if total_pool <= 0 or not bottom:
                    return "분배할 금액이 없습니다."

                per = total_pool // len(bottom)
                remainder = total_pool % len(bottom)
                for i, p in enumerate(bottom):
                    give = per + (1 if i < remainder else 0)
                    await connection.execute(
                        "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2",
                        give, str(p["user_id"])
                    )
        finally:
            await connection.close()

        public = discord.Embed(
            title="🕊️ 천사의 구제가 발동되었습니다!",
            description=(
                f"상위 플레이어의 재산 일부가 하위 플레이어들에게 분배되었습니다.\n"
                f"총 분배 금액: **{total_pool:,}** 코인"
            ),
            color=discord.Color.blue()
        )
        await interaction.channel.send(embed=public)
        return f"🕊️ 총 **{total_pool:,}** 코인이 분배되었습니다!"

    async def _activate_recover_loss(self, interaction) -> str:
        """신의 모래시계: 최근 손실 복구"""
        player = await get_player(str(interaction.user.id), str(interaction.guild.id))
        loss = player.get("last_money_loss") or 0
        if loss <= 0:
            return "⚠️ 복구할 최근 손실이 없습니다."

        connection = await get_db()
        try:
            new_money = await connection.fetchval(
                """
                UPDATE players
                SET money = money + $1,
                    last_money_loss = 0,
                    updated_at = NOW()
                WHERE user_id = $2
                RETURNING money
                """,
                loss, str(interaction.user.id)
            )
        finally:
            await connection.close()

        public = discord.Embed(
            title="⏳ 신의 모래시계가 발동되었습니다!",
            description=(
                f"<@{interaction.user.id}> 님이 최근 손실 **{loss:,}** 코인을 복구했습니다!"
            ),
            color=discord.Color.gold()
        )
        await interaction.channel.send(embed=public)
        return f"⏳ **{loss:,}** 코인을 복구했습니다! (현재: {new_money:,})"


# ============================================================
# 다이아 상점 View
# ============================================================

class DiamondShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        options = []
        for key, item in DIAMOND_SHOP_ITEMS.items():
            options.append(discord.SelectOption(
                label=f"{item['desc']} ({item['price']}💎)",
                value=key,
                description=f"+{item['bonus_money']:,} 시작 자금",
                emoji=item["emoji"]
            ))
        self.select = discord.ui.Select(
            placeholder="구매할 시작 자금 패키지를 선택하세요",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        key = self.select.values[0]
        item = DIAMOND_SHOP_ITEMS.get(key)
        if not item:
            await interaction.response.send_message("존재하지 않는 상품입니다.", ephemeral=True)
            return

        # 진행 중 게임인지 확인 → 진행 중이면 구매 불가
        playing = await get_playing_game(interaction.channel.id)
        if playing:
            await interaction.response.send_message(
                "🔒 게임이 이미 시작된 후에는 다이아 상점을 이용할 수 없습니다.\n"
                "다음 서바이벌 시작 전에 이용해주세요.",
                ephemeral=True
            )
            return

        user_id = interaction.user.id
        lock = action_lock(user_id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            player = await get_or_create_player(interaction.user, interaction.guild)
            if player["diamonds"] < item["price"]:
                await interaction.response.send_message(
                    f"🔴 다이아가 부족합니다.\n필요: **{item['price']}** / 보유: **{player['diamonds']}**",
                    ephemeral=True
                )
                return

            connection = await get_db()
            try:
                new_diamonds = await connection.fetchval(
                    """
                    UPDATE players
                    SET diamonds = diamonds - $1,
                        bonus_starting_money = COALESCE(bonus_starting_money, 0) + $2,
                        updated_at = NOW()
                    WHERE server_id = $3 AND user_id = $4 AND diamonds >= $1
                    RETURNING diamonds
                    """,
                    item["price"], item["bonus_money"],
                    str(interaction.guild.id), str(interaction.user.id)
                )
                if new_diamonds is None:
                    await interaction.response.send_message("구매 실패 (다이아 부족)", ephemeral=True)
                    return
            finally:
                await connection.close()

            await interaction.response.send_message(
                f"{item['emoji']} **시작 자금 보너스 구매 완료!**\n"
                f"💎 -**{item['price']}** 다이아\n"
                f"🪙 다음 게임 시작 시 **+{item['bonus_money']:,}** 코인 추가\n"
                f"💎 남은 다이아: **{new_diamonds}**\n\n"
                f"※ 게임 시작 시 자동 적용되며, 보너스는 초기화됩니다.",
                ephemeral=True
            )


# ============================================================
# 아이템 가방 View
# ============================================================

class ItemBagView(discord.ui.View):
    def __init__(self, game_id, inventory: dict):
        super().__init__(timeout=120)
        self.game_id = game_id
        self.inventory = inventory

        options = []
        for name, count in inventory.items():
            if count <= 0:
                continue
            item = SHOP_ITEMS.get(name, {})
            emoji = item.get("emoji", "📦")
            options.append(discord.SelectOption(
                label=f"{name} ×{count}",
                value=name,
                description=item.get("desc", "아이템")[:50],
                emoji=emoji
            ))

        if options:
            self.select = discord.ui.Select(
                placeholder="사용할 아이템을 선택하세요",
                options=options[:25],  # Discord 제한
                min_values=1,
                max_values=1
            )
            self.select.callback = self.on_select
            self.add_item(self.select)
        else:
            # 아이템이 없을 때 더미
            pass

    async def interaction_check(self, interaction):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "playing":
            await interaction.response.send_message("🔒 게임이 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        item_name = self.select.values[0]
        item = SHOP_ITEMS.get(item_name)
        if not item:
            await interaction.response.send_message("알 수 없는 아이템입니다.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        lock = action_lock(interaction.user.id)
        if lock.locked():
            await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)
            return

        async with lock:
            # 보유 확인
            inv = await get_player_inventory(user_id, str(interaction.guild.id))
            if inv.get(item_name, 0) <= 0:
                await interaction.response.send_message("해당 아이템을 보유하고 있지 않습니다.", ephemeral=True)
                return

            # 아이템별 사용 처리
            result_msg = await self._use_item(interaction, item_name, item)

            if result_msg is None:
                return  # 이미 response 보낸 경우

            # 성공 시 아이템 1개 소모
            await remove_item_from_inventory(user_id, str(interaction.guild.id), item_name, 1)

            await interaction.response.send_message(
                f"{item.get('emoji', '📦')} **{item_name}** 사용 완료!\n{result_msg}",
                ephemeral=True
            )

    async def _use_item(self, interaction, item_name: str, item: dict) -> str | None:
        """아이템 효과 발동. 성공 메시지 반환 또는 None (직접 response한 경우)"""
        game_id = self.game_id
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild.id)

        if item_name == "시간 연장권":
            # 다음 탈락을 3분 연장
            game = await get_game_by_id(game_id)
            if not game:
                await interaction.response.send_message("게임 정보를 찾을 수 없습니다.", ephemeral=True)
                return None
            game_data = parse_game_data(game["game_data"])
            next_str = game_data.get("next_elimination_at")
            if not next_str:
                await interaction.response.send_message("탈락 시간 정보가 없습니다.", ephemeral=True)
                return None
            try:
                next_time = datetime.fromisoformat(next_str)
                new_time = next_time + timedelta(minutes=3)
                connection = await get_db()
                try:
                    await connection.execute(
                        """
                        UPDATE games
                        SET game_data = jsonb_set(
                            COALESCE(game_data, '{}'::jsonb),
                            '{next_elimination_at}',
                            to_jsonb($2::text),
                            TRUE
                        )
                        WHERE id = $1
                        """,
                        game_id, new_time.isoformat()
                    )
                finally:
                    await connection.close()

                # 공개 알림
                embed = discord.Embed(
                    title="⏳ 시간 연장권 사용!",
                    description=f"<@{user_id}> 님이 다음 탈락 판정을 **3분 연장**했습니다!",
                    color=discord.Color.blue()
                )
                await interaction.channel.send(embed=embed)
                return "다음 탈락 판정이 **3분** 연장되었습니다."
            except Exception as e:
                await interaction.response.send_message(f"연장 실패: {e}", ephemeral=True)
                return None

        elif item_name == "정찰권":
            # 간단한 정보 제공 (현재는 꼴등 정보 정도)
            players = await get_game_players(game_id)
            alive = [p for p in players if p["alive"] and not p["eliminated"]]
            if not alive:
                return "생존자가 없습니다."
            lowest = min(alive, key=lambda p: p["money"])
            highest = max(alive, key=lambda p: p["money"])
            return (
                f"👁️ 정찰 결과\n"
                f"현재 꼴등: <@{lowest['user_id']}> (**{lowest['money']:,}**)\n"
                f"현재 1등: <@{highest['user_id']}> (**{highest['money']:,}**)"
            )

        elif item_name == "빨대 쪼옵":
            # 랜덤 생존자 소액 갈취
            players = await get_game_players(game_id)
            others = [p for p in players if p["alive"] and not p["eliminated"] and str(p["user_id"]) != user_id]
            if not others:
                return "가져올 대상이 없습니다."
            target = random.choice(others)
            steal = random.randint(10_000, min(100_000, max(10_000, target["money"] // 10)))
            if target["money"] < steal:
                steal = target["money"]
            if steal <= 0:
                return "상대에게 가져올 돈이 없습니다."

            connection = await get_db()
            try:
                await connection.execute(
                    "UPDATE players SET money = money - $1 WHERE user_id = $2",
                    steal, str(target["user_id"])
                )
                await connection.execute(
                    "UPDATE players SET money = money + $1 WHERE user_id = $2",
                    steal, user_id
                )
            finally:
                await connection.close()

            embed = discord.Embed(
                title="🪣 빨대 쪼옵!",
                description=f"<@{user_id}> 님이 <@{target['user_id']}> 님에게서 **{steal:,}** 코인을 가져갔습니다!",
                color=discord.Color.orange()
            )
            await interaction.channel.send(embed=embed)
            return f"<@{target['user_id']}> 에게서 **{steal:,}** 코인을 가져왔습니다."

        elif item_name == "랜덤박스":
            # 간단 랜덤박스 개봉
            roll = random.random()
            if roll < 0.05:
                reward = random.randint(800_000, 1_200_000)
                msg = f"🎉 대박! **{reward:,}** 코인 획득!"
            elif roll < 0.25:
                reward = random.randint(300_000, 600_000)
                msg = f"✨ 성공! **{reward:,}** 코인 획득!"
            elif roll < 0.55:
                reward = random.randint(100_000, 250_000)
                msg = f"보통... **{reward:,}** 코인 획득"
            elif roll < 0.80:
                reward = random.randint(10_000, 80_000)
                msg = f"아쉬움... **{reward:,}** 코인"
            else:
                reward = 0
                msg = "💥 꽝... 아무것도 없습니다."

            if reward > 0:
                connection = await get_db()
                try:
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2",
                        reward, user_id
                    )
                finally:
                    await connection.close()
            return msg

        elif item_name == "밑장빼기권":
            return (
                "🎭 밑장빼기권은 **카드 게임(블랙잭, 에이스 브레이커, 인디언 포커 등) 시작 전 10초**에 "
                "자동으로 사용 여부를 선택할 수 있습니다.\n"
                "지금은 가방에서 미리 준비만 해두세요. (아이템은 소모되지 않았습니다)"
            )

        elif item_name == "이벤트 참가권":
            return (
                "🎟️ 이벤트 참가권은 **보물찾기 등 이벤트**가 열렸을 때 사용됩니다.\n"
                "현재 진행 중인 이벤트가 없어 사용할 수 없습니다. (아이템은 소모되지 않았습니다)"
            )

        elif item_name == "경매 주최권":
            # 미스터리 코인 상자 경매 시작
            embed = discord.Embed(
                title="🚨 SPECIAL EVENT: 미스터리 코인 상자 등장!",
                description=(
                    f"<@{user_id}> 님이 **경매 주최권**을 사용했습니다!\n\n"
                    "🎁 상자 내용물: 🪙 ??? 코인 (대박 or 꽝!)\n"
                    "🏁 시작가: **100,000** 코인\n"
                    "📈 입찰 단위: +50,000 / +200,000\n"
                    "⏱️ 입찰 시간: 60초 (입찰 시 5초 연장)\n\n"
                    "10초 후 경매가 시작됩니다..."
                ),
                color=discord.Color.gold()
            )
            await interaction.channel.send(embed=embed)

            async def _start_auction():
                await asyncio.sleep(10)
                auction_view = MysteryAuctionView(self.game_id, str(user_id))
                msg = await interaction.channel.send(
                    embed=auction_view.build_embed(),
                    view=auction_view
                )
                auction_view.message = msg
                asyncio.create_task(auction_view.run_timer())

            asyncio.create_task(_start_auction())
            return "미스터리 코인 상자 경매가 10초 후 시작됩니다!"

        else:
            return "이 아이템은 아직 사용 기능이 구현되지 않았습니다."


# ============================================================
# 미스터리 코인 상자 경매
# ============================================================

class MysteryAuctionView(discord.ui.View):
    def __init__(self, game_id, host_id: str):
        super().__init__(timeout=180)
        self.game_id = game_id
        self.host_id = host_id
        self.current_bid = 100_000
        self.highest_bidder = None  # user_id
        self.highest_name = None
        self.ends_at = datetime.utcnow() + timedelta(seconds=60)
        self.finished = False
        self.message = None
        self.lock = asyncio.Lock()

    def build_embed(self):
        remaining = max(0, int((self.ends_at - datetime.utcnow()).total_seconds()))
        bidder = f"<@{self.highest_bidder}>" if self.highest_bidder else "없음"
        embed = discord.Embed(
            title="🔨 미스터리 코인 상자 실시간 경매!",
            description=(
                "🎁 상자 구성: 🪙 ??? 코인 (초대박 vs 10 코인 꽝)\n"
                f"🏁 현재 최고가: **{self.current_bid:,}** 코인\n"
                f"👤 최고 입찰자: {bidder}\n"
                f"⏱️ 남은 시간: **{remaining}초**"
            ),
            color=discord.Color.gold()
        )
        embed.set_footer(text="입찰 시 남은 시간이 5초 미만이면 5초로 연장됩니다.")
        return embed

    async def run_timer(self):
        try:
            while not self.finished:
                await asyncio.sleep(2)
                remaining = (self.ends_at - datetime.utcnow()).total_seconds()
                if remaining <= 0:
                    await self.finalize()
                    break
                if self.message and remaining < 50:
                    try:
                        await self.message.edit(embed=self.build_embed(), view=self)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def place_bid(self, interaction: discord.Interaction, amount: int):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("이미 종료된 경매입니다.", ephemeral=True)
                return
            if self.highest_bidder and str(interaction.user.id) == str(self.highest_bidder):
                await interaction.response.send_message("이미 최고 입찰자입니다.", ephemeral=True)
                return

            player = await get_player(str(interaction.user.id), str(interaction.guild.id))
            if not player or not player["alive"] or player["eliminated"]:
                await interaction.response.send_message("생존자만 입찰할 수 있습니다.", ephemeral=True)
                return
            if player["money"] < amount:
                await interaction.response.send_message(
                    f"코인이 부족합니다. 보유: **{player['money']:,}**", ephemeral=True
                )
                return

            # 이전 최고 입찰자 환불은 finalize에서 처리하지 않고, 낙찰자만 차감
            self.current_bid = amount
            self.highest_bidder = str(interaction.user.id)
            self.highest_name = interaction.user.display_name

            # 시간 연장
            remaining = (self.ends_at - datetime.utcnow()).total_seconds()
            if remaining < 5:
                self.ends_at = datetime.utcnow() + timedelta(seconds=5)

            await interaction.response.send_message(
                f"💥 <@{interaction.user.id}> 님이 **{amount:,}** 코인으로 입찰!",
                ephemeral=False
            )
            if self.message:
                try:
                    await self.message.edit(embed=self.build_embed(), view=self)
                except Exception:
                    pass

    async def finalize(self):
        if self.finished:
            return
        self.finished = True
        self.stop()
        for child in self.children:
            child.disabled = True

        if not self.highest_bidder:
            if self.message:
                embed = discord.Embed(
                    title="🔨 경매 유찰",
                    description="입찰자가 없어 경매가 취소되었습니다.",
                    color=discord.Color.dark_grey()
                )
                try:
                    await self.message.edit(embed=embed, view=None)
                except Exception:
                    pass
            return

        # 낙찰자 차감
        connection = await get_db()
        try:
            ok = await connection.fetchval(
                """
                UPDATE players SET money = money - $1, updated_at = NOW()
                WHERE user_id = $2 AND money >= $1 RETURNING money
                """,
                self.current_bid, self.highest_bidder
            )
            if ok is None:
                # 잔액 부족 시 유찰 처리
                if self.message:
                    await self.message.edit(
                        content="낙찰자의 잔액이 부족하여 경매가 취소되었습니다.",
                        embed=None, view=None
                    )
                return
        finally:
            await connection.close()

        # 상자 결과 확률 (기획안 기준)
        roll = random.random()
        if roll < 0.01:
            reward = 15_000_000 + random.randint(0, 5_000_000)  # 주작급
            grade = "🏆 주작 (Wtf)!!!"
        elif roll < 0.15:
            reward = random.randint(5_000_000, 15_000_000)
            grade = "💎 초대박 JACKPOT!"
        elif roll < 0.50:
            reward = random.randint(2_000_000, 4_000_000)
            grade = "🎉 대박!"
        elif roll < 0.80:
            reward = random.randint(500_000, 1_500_000)
            grade = "✨ 본전치기급"
        else:
            reward = random.randint(10, 1_000)
            grade = "💣 대박 꽝 (Trap)..."

        connection = await get_db()
        try:
            new_money = await connection.fetchval(
                "UPDATE players SET money = money + $1 WHERE user_id = $2 RETURNING money",
                reward, self.highest_bidder
            )
        finally:
            await connection.close()

        embed = discord.Embed(
            title="🔨 탕! 탕! 탕! 낙찰!",
            description=(
                f"🎉 최종 낙찰자: <@{self.highest_bidder}> (**{self.current_bid:,}** 코인 차감)\n\n"
                f"📦 상자를 열어보는 중...\n"
                f"💥 상자 결과: **{grade}**\n"
                f"🪙 **{reward:,}** 코인 획득!\n"
                f"현재 보유: **{new_money:,}** 코인"
            ),
            color=discord.Color.gold()
        )
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass
        # 채널에도 한 번 더
        try:
            ch = self.message.channel if self.message else None
            if ch:
                await ch.send(embed=embed)
        except Exception:
            pass

    @discord.ui.button(label="+50,000 입찰", emoji="✋", style=discord.ButtonStyle.primary)
    async def bid_50k(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_bid = self.current_bid + 50_000
        await self.place_bid(interaction, new_bid)

    @discord.ui.button(label="+200,000 찌르기", emoji="🚀", style=discord.ButtonStyle.danger)
    async def bid_200k(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_bid = self.current_bid + 200_000
        await self.place_bid(interaction, new_bid)


# ============================================================
# 블랙잭
# ============================================================

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def create_deck(num_decks=2):
    deck = []
    for _ in range(num_decks):
        for s in SUITS:
            for r in RANKS:
                deck.append(f"{r}{s}")
    random.shuffle(deck)
    return deck


def card_value(card):
    rank = card[:-1]
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_value(hand):
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c.startswith("A"))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_hand(hand, hide_first=False):
    if hide_first and len(hand) >= 1:
        return "🂠 " + " ".join(hand[1:])
    return " ".join(hand)


class BlackjackView(discord.ui.View):
    def __init__(self, game_id, user_id, guild_id, bet: int):
        super().__init__(timeout=120)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.deck = create_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.doubled = False

    def build_embed(self, reveal=False):
        p_val = hand_value(self.player_hand)
        d_val = hand_value(self.dealer_hand) if reveal else card_value(self.dealer_hand[0])
        embed = discord.Embed(title="🃏 블랙잭", color=discord.Color.dark_green())
        embed.add_field(
            name=f"딜러 {'(' + str(hand_value(self.dealer_hand)) + ')' if reveal else ''}",
            value=format_hand(self.dealer_hand, hide_first=not reveal),
            inline=False
        )
        embed.add_field(
            name=f"플레이어 ({p_val})",
            value=format_hand(self.player_hand),
            inline=False
        )
        embed.add_field(name="배팅", value=f"**{self.bet:,}** 코인", inline=True)
        return embed

    async def end_game(self, interaction, result: str, payout_mult: float):
        if self.finished:
            return
        self.finished = True
        self.stop()
        for child in self.children:
            child.disabled = True

        payout = int(self.bet * payout_mult)
        connection = await get_db()
        try:
            if payout > 0:
                new_money = await connection.fetchval(
                    "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2 RETURNING money",
                    payout, str(self.user_id)
                )
            elif payout < 0:
                # 이미 배팅 시 차감했으므로 추가 차감 없음. 패배 시 0
                new_money = await connection.fetchval(
                    "SELECT money FROM players WHERE user_id = $1", str(self.user_id)
                )
            else:
                # push - 배팅 반환
                new_money = await connection.fetchval(
                    "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2 RETURNING money",
                    self.bet, str(self.user_id)
                )
        finally:
            await connection.close()

        embed = self.build_embed(reveal=True)
        embed.add_field(name="결과", value=result, inline=False)
        if payout > 0:
            embed.add_field(name="획득", value=f"+**{payout:,}** 코인", inline=True)
        elif payout == 0 and "무승부" in result:
            embed.add_field(name="반환", value=f"**{self.bet:,}** 코인 반환", inline=True)
        else:
            embed.add_field(name="손실", value=f"-**{self.bet:,}** 코인", inline=True)
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)

        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="HIT", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        self.player_hand.append(self.deck.pop())
        val = hand_value(self.player_hand)
        if val > 21:
            await self.end_game(interaction, "💥 버스트! 패배", 0)
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="STAND", emoji="🛑", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        # 딜러 플레이
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())
        p = hand_value(self.player_hand)
        d = hand_value(self.dealer_hand)
        if d > 21:
            await self.end_game(interaction, "🎉 딜러 버스트! 승리", 2.0)
        elif p > d:
            await self.end_game(interaction, "🎉 승리!", 2.0)
        elif p < d:
            await self.end_game(interaction, "😢 패배", 0)
        else:
            await self.end_game(interaction, "🤝 무승부", 1.0)

    @discord.ui.button(label="DOUBLE", emoji="💰", style=discord.ButtonStyle.success)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished or self.doubled:
            await interaction.response.defer()
            return
        if len(self.player_hand) != 2:
            await interaction.response.send_message("첫 두 장일 때만 DOUBLE 가능합니다.", ephemeral=True)
            return
        # 추가 배팅 가능 여부
        player = await get_player(str(self.user_id), str(self.guild_id))
        if not player or player["money"] < self.bet:
            await interaction.response.send_message("DOUBLE 할 코인이 부족합니다.", ephemeral=True)
            return
        connection = await get_db()
        try:
            await connection.execute(
                "UPDATE players SET money = money - $1 WHERE user_id = $2 AND money >= $1",
                self.bet, str(self.user_id)
            )
        finally:
            await connection.close()
        self.bet *= 2
        self.doubled = True
        self.player_hand.append(self.deck.pop())
        val = hand_value(self.player_hand)
        if val > 21:
            await self.end_game(interaction, "💥 버스트! 패배 (DOUBLE)", 0)
        else:
            # 자동 STAND
            while hand_value(self.dealer_hand) < 17:
                self.dealer_hand.append(self.deck.pop())
            p = hand_value(self.player_hand)
            d = hand_value(self.dealer_hand)
            if d > 21:
                await self.end_game(interaction, "🎉 딜러 버스트! 승리 (DOUBLE)", 2.0)
            elif p > d:
                await self.end_game(interaction, "🎉 승리! (DOUBLE)", 2.0)
            elif p < d:
                await self.end_game(interaction, "😢 패배 (DOUBLE)", 0)
            else:
                await self.end_game(interaction, "🤝 무승부 (DOUBLE)", 1.0)

    @discord.ui.button(label="SURRENDER", emoji="🏳️", style=discord.ButtonStyle.danger)
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        if len(self.player_hand) != 2:
            await interaction.response.send_message("첫 두 장일 때만 SURRENDER 가능합니다.", ephemeral=True)
            return
        # 절반 반환
        half = self.bet // 2
        connection = await get_db()
        try:
            new_money = await connection.fetchval(
                "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2 RETURNING money",
                half, str(self.user_id)
            )
        finally:
            await connection.close()
        self.finished = True
        self.stop()
        embed = self.build_embed(reveal=True)
        embed.add_field(name="결과", value="🏳️ SURRENDER (절반 반환)", inline=False)
        embed.add_field(name="반환", value=f"**{half:,}** 코인", inline=True)
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)
        await interaction.response.edit_message(embed=embed, view=None)


class BlackjackBetModal(discord.ui.Modal, title="🃏 블랙잭 배팅"):
    def __init__(self, game_id):
        super().__init__()
        self.game_id = game_id
        self.amount = discord.ui.TextInput(
            label="배팅 금액",
            placeholder="예: 50000 (최소 10,000)",
            min_length=1,
            max_length=12,
            required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet = int(self.amount.value.replace(",", "").strip())
            if bet < 10_000:
                await interaction.response.send_message("최소 배팅액은 10,000 코인입니다.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("올바른 숫자를 입력하세요.", ephemeral=True)
            return

        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 게임을 할 수 없습니다.", ephemeral=True)
            return
        if player["money"] < bet:
            await interaction.response.send_message(
                f"코인이 부족합니다. 보유: **{player['money']:,}**", ephemeral=True
            )
            return

        # 배팅 차감
        connection = await get_db()
        try:
            ok = await connection.fetchval(
                "UPDATE players SET money = money - $1 WHERE user_id = $2 AND money >= $1 RETURNING money",
                bet, str(interaction.user.id)
            )
            if ok is None:
                await interaction.response.send_message("배팅 실패 (잔액 부족)", ephemeral=True)
                return
        finally:
            await connection.close()

        view = BlackjackView(self.game_id, interaction.user.id, interaction.guild.id, bet)
        # 자연 블랙잭 체크
        if hand_value(view.player_hand) == 21:
            payout = int(bet * 2.5)
            connection = await get_db()
            try:
                new_money = await connection.fetchval(
                    "UPDATE players SET money = money + $1 WHERE user_id = $2 RETURNING money",
                    payout, str(interaction.user.id)
                )
            finally:
                await connection.close()
            embed = view.build_embed(reveal=True)
            embed.add_field(name="결과", value="🎉 **블랙잭!** (×2.5)", inline=False)
            embed.add_field(name="획득", value=f"+**{payout:,}**", inline=True)
            embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


# ============================================================
# 에이스 브레이커 (간소화 버전)
# ============================================================

class AceBreakerView(discord.ui.View):
    """
    간소화된 에이스 브레이커 (1인 vs 봇)
    카드: 2~9, ACE, JOKER
    비교: 높음 → 낮음 → 높음
    """
    def __init__(self, game_id, user_id, guild_id, bet: int):
        super().__init__(timeout=90)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.finished = False
        self.player_cards = self._draw_hand()
        self.bot_cards = self._draw_hand()
        self.revealed = False

    def _draw_hand(self):
        # 2~9, A, J (JOKER)
        pool = [str(i) for i in range(2, 10)] + ["A", "J"] * 2
        random.shuffle(pool)
        hand = []
        joker_count = 0
        while len(hand) < 3:
            c = pool.pop()
            if c == "J":
                if joker_count >= 1:
                    continue
                joker_count += 1
            hand.append(c)
        return hand

    def card_str(self, c):
        if c == "A":
            return "🅰️ ACE"
        if c == "J":
            return "🃏 JOKER"
        return f"**{c}**"

    def compare_round(self, p, b, want_higher: bool):
        """한 라운드 비교. want_higher=True면 플레이어가 높아야 이김"""
        # ACE vs JOKER 특수
        if p == "A" and b == "J":
            return False  # JOKER가 ACE를 이김
        if b == "A" and p == "J":
            return True
        if p == "A":
            return True
        if b == "A":
            return False
        if p == "J":
            return False  # JOKER는 숫자 비교에서 약함 (ACE 막는 용도)
        if b == "J":
            return True
        # 숫자 비교
        if want_higher:
            return int(p) > int(b)
        return int(p) < int(b)

    def build_embed(self, show_bot=False):
        embed = discord.Embed(title="🃏 에이스 브레이커", color=discord.Color.purple())
        p_str = " | ".join(self.card_str(c) for c in self.player_cards)
        if show_bot:
            b_str = " | ".join(self.card_str(c) for c in self.bot_cards)
        else:
            b_str = "🂠 | 🂠 | 🂠"
        embed.add_field(name="당신의 패", value=p_str, inline=False)
        embed.add_field(name="상대 패", value=b_str, inline=False)
        embed.add_field(name="배팅", value=f"**{self.bet:,}** 코인", inline=True)
        embed.set_footer(text="높음 → 낮음 → 높음 순서로 비교 / ACE는 무조건 승 (단 JOKER에 패배)")
        return embed

    @discord.ui.button(label="대결 시작!", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def fight(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        self.finished = True
        self.stop()

        # 3라운드 비교
        results = []
        wins = 0
        # 1: 높음, 2: 낮음, 3: 높음
        for i, want_high in enumerate([True, False, True]):
            p = self.player_cards[i]
            b = self.bot_cards[i]
            win = self.compare_round(p, b, want_high)
            results.append(win)
            if win:
                wins += 1

        ace_count = self.player_cards.count("A")
        joker_break = any(
            self.player_cards[i] == "J" and self.bot_cards[i] == "A" for i in range(3)
        )

        if wins >= 2:
            mult = 2.0
            if ace_count >= 1:
                mult = min(3.1, 2.0 + ace_count * 0.4)
            if joker_break:
                mult = max(mult, 2.1)
            payout = int(self.bet * mult)
            result = f"🎉 승리! ({wins}/3) ×{mult:.1f}"
        else:
            payout = 0
            result = f"😢 패배 ({wins}/3)"

        connection = await get_db()
        try:
            if payout > 0:
                new_money = await connection.fetchval(
                    "UPDATE players SET money = money + $1 WHERE user_id = $2 RETURNING money",
                    payout, str(self.user_id)
                )
            else:
                new_money = await connection.fetchval(
                    "SELECT money FROM players WHERE user_id = $1", str(self.user_id)
                )
        finally:
            await connection.close()

        embed = self.build_embed(show_bot=True)
        detail = []
        for i, (w, want) in enumerate(zip(results, ["높음", "낮음", "높음"])):
            mark = "✅" if w else "❌"
            detail.append(f"{i+1}라운드({want}): {mark}")
        embed.add_field(name="라운드 결과", value="\n".join(detail), inline=False)
        embed.add_field(name="최종", value=result, inline=False)
        if payout > 0:
            embed.add_field(name="획득", value=f"+**{payout:,}**", inline=True)
        else:
            embed.add_field(name="손실", value=f"-**{self.bet:,}**", inline=True)
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)
        await interaction.response.edit_message(embed=embed, view=None)


class AceBreakerBetModal(discord.ui.Modal, title="🃏 에이스 브레이커 배팅"):
    def __init__(self, game_id):
        super().__init__()
        self.game_id = game_id
        self.amount = discord.ui.TextInput(
            label="배팅 금액",
            placeholder="예: 100000 (최소 20,000)",
            min_length=1,
            max_length=12,
            required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet = int(self.amount.value.replace(",", "").strip())
            if bet < 20_000:
                await interaction.response.send_message("최소 배팅액은 20,000 코인입니다.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("올바른 숫자를 입력하세요.", ephemeral=True)
            return

        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 게임을 할 수 없습니다.", ephemeral=True)
            return
        if player["money"] < bet:
            await interaction.response.send_message(
                f"코인이 부족합니다. 보유: **{player['money']:,}**", ephemeral=True
            )
            return

        connection = await get_db()
        try:
            ok = await connection.fetchval(
                "UPDATE players SET money = money - $1 WHERE user_id = $2 AND money >= $1 RETURNING money",
                bet, str(interaction.user.id)
            )
            if ok is None:
                await interaction.response.send_message("배팅 실패", ephemeral=True)
                return
        finally:
            await connection.close()

        view = AceBreakerView(self.game_id, interaction.user.id, interaction.guild.id, bet)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


# ============================================================
# 게임 선택 메뉴
# ============================================================

class GameSelectView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=120)
        self.game_id = game_id

    @discord.ui.button(label="블랙잭", emoji="🃏", style=discord.ButtonStyle.primary, row=0)
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = BlackjackBetModal(self.game_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="에이스 브레이커", emoji="🅰️", style=discord.ButtonStyle.primary, row=0)
    async def ace_breaker(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AceBreakerBetModal(self.game_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="홀짝 (간단)", emoji="🔢", style=discord.ButtonStyle.secondary, row=1)
    async def odd_even(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🔢 홀짝 게임은 다음 업데이트에서 본격 구현됩니다.\n현재는 블랙잭 / 에이스 브레이커를 즐겨주세요!",
            ephemeral=True
        )

    @discord.ui.button(label="닫기", emoji="❌", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="게임 메뉴를 닫았습니다.", embed=None, view=None)


# ============================================================
# 메인 게임 View (SurvivalGameView)
# ============================================================

class SurvivalGameView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=None)
        self.game_id = game_id

    async def interaction_check(self, interaction):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "playing":
            await interaction.response.send_message("🔒 이 게임은 더 이상 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="새로고침", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await build_main_embed(self.game_id, interaction.user.id, interaction.guild.id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="게임", emoji="🎮", style=discord.ButtonStyle.success, row=0)
    async def games(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 게임을 할 수 없습니다.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🎮 게임 메뉴",
            description=(
                "원하는 게임을 선택하세요.\n"
                "배팅 금액은 게임 시작 시 입력합니다.\n\n"
                f"🪙 현재 코인: **{player['money']:,}**"
            ),
            color=discord.Color.green()
        )
        embed.add_field(name="🃏 블랙잭", value="HIT / STAND / DOUBLE / SURRENDER", inline=False)
        embed.add_field(name="🅰️ 에이스 브레이커", value="높음→낮음→높음 심리전 (ACE/JOKER)", inline=False)
        embed.add_field(name="🔢 홀짝 외", value="추가 게임 순차 업데이트 예정", inline=False)
        await interaction.response.send_message(embed=embed, view=GameSelectView(self.game_id), ephemeral=True)

    @discord.ui.button(label="알바", emoji="🧑‍💼", style=discord.ButtonStyle.primary, row=0)
    async def jobs(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        remaining = get_job_remaining(interaction.user.id)

        cooldown_text = (
            f"⏳ 쿨타임: **{format_seconds(remaining)}**"
            if remaining > 0 else "🟢 지금 바로 가능"
        )

        embed = discord.Embed(
            title="🧑‍💼 알바",
            description=f"알바를 해서 코인을 벌 수 있습니다.\n\n{cooldown_text}\n한 번 하면 **5분** 쿨타임"
        )
        for name, job in JOBS.items():
            embed.add_field(name=f"{job['emoji']} {name}", value=f"{job['reward']:,} 코인", inline=True)
        embed.set_footer(text=f"현재 코인: {player['money']:,}")

        await interaction.response.send_message(embed=embed, view=JobView(self.game_id), ephemeral=True)

    @discord.ui.button(label="상점", emoji="🏪", style=discord.ButtonStyle.primary, row=1)
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        embed = discord.Embed(
            title="🏪 일반 상점",
            description=(
                "서바이벌 중 아이템을 구매할 수 있습니다.\n"
                "아이템은 **미니게임 시작 전 10초** 동안만 사용 여부를 결정할 수 있습니다.\n\n"
                f"🪙 현재 코인: **{player['money']:,}**"
            ),
            color=discord.Color.blue()
        )
        for name, item in SHOP_ITEMS.items():
            embed.add_field(
                name=f"{item['emoji']} {name}",
                value=f"**{item['price']:,}** 코인\n{item['desc']}",
                inline=False
            )
        await interaction.response.send_message(
            embed=embed, view=ShopView(self.game_id), ephemeral=True
        )

    @discord.ui.button(label="기부", emoji="😇", style=discord.ButtonStyle.primary, row=1)
    async def donate(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 기부할 수 없습니다.", ephemeral=True)
            return

        # 현재 꼴등 미리 보여주기
        players = await get_game_players(self.game_id)
        alive = [p for p in players if p["alive"] and not p["eliminated"]]
        others = [p for p in alive if str(p["user_id"]) != str(interaction.user.id)]
        lowest_text = "없음"
        if others:
            lowest = min(others, key=lambda p: p["money"])
            lowest_text = f"<@{lowest['user_id']}> (**{lowest['money']:,}** 코인)"

        embed = discord.Embed(
            title="😇 기부 시스템",
            description=(
                "현재 **꼴등**에게 코인을 기부하면 선행 포인트를 얻습니다.\n"
                "부자가 꼴등을 살려주는 전략이 가능합니다.\n\n"
                f"🎯 현재 꼴등: {lowest_text}\n"
                f"🪙 내 코인: **{player['money']:,}**\n"
                f"😇 내 선행 포인트: **{player['good_deed']:,}**"
            ),
            color=discord.Color.green()
        )
        await interaction.response.send_message(
            embed=embed, view=DonateView(self.game_id), ephemeral=True
        )

    @discord.ui.button(label="천사의 상점", emoji="🪽", style=discord.ButtonStyle.success, row=2)
    async def angel_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 천사의 상점을 이용할 수 없습니다.", ephemeral=True)
            return

        already = player.get("angel_item")
        already_text = f"⚠️ 이미 **{already}** 구매함 (추가 구매 불가)" if already else "🟢 아직 구매하지 않음 (한 게임당 1개)"

        embed = discord.Embed(
            title="😇 천사의 상점",
            description=(
                "선행 포인트로 강력한 아이템을 구매할 수 있습니다.\n"
                "**⚠️ 한 게임에서 단 하나의 아이템만 구매 가능합니다.**\n\n"
                f"😇 보유 선행 포인트: **{player.get('good_deed') or 0:,}P**\n"
                f"{already_text}"
            ),
            color=discord.Color.purple()
        )
        for name, item in ANGEL_SHOP_ITEMS.items():
            embed.add_field(
                name=f"{item['emoji']} {name}",
                value=f"**{item['price']:,}P**\n{item['desc']}",
                inline=False
            )
        await interaction.response.send_message(
            embed=embed, view=AngelShopView(self.game_id), ephemeral=True
        )

    @discord.ui.button(label="내 정보", emoji="👤", style=discord.ButtonStyle.secondary, row=1)
    async def myinfo(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await build_main_embed(self.game_id, interaction.user.id, interaction.guild.id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="가방", emoji="🎒", style=discord.ButtonStyle.primary, row=2)
    async def bag(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        inv = parse_inventory(player.get("inventory"))
        angel = player.get("angel_item")

        if not inv and not angel:
            await interaction.response.send_message(
                "🎒 가방이 비어 있습니다.\n상점에서 아이템을 구매하세요!",
                ephemeral=True
            )
            return

        lines = []
        if inv:
            for name, cnt in inv.items():
                emoji = SHOP_ITEMS.get(name, {}).get("emoji", "📦")
                lines.append(f"{emoji} **{name}** ×{cnt}")
        if angel:
            emoji = ANGEL_SHOP_ITEMS.get(angel, {}).get("emoji", "🪽")
            lines.append(f"{emoji} **{angel}** (천사 아이템 - 자동 발동)")

        embed = discord.Embed(
            title="🎒 아이템 가방",
            description="\n".join(lines) + "\n\n사용할 아이템을 아래에서 선택하세요.",
            color=discord.Color.teal()
        )
        view = ItemBagView(self.game_id, inv) if inv else None
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="랭킹", emoji="🏆", style=discord.ButtonStyle.secondary, row=2)
    async def ranking(self, interaction: discord.Interaction, button: discord.ui.Button):
        players = await get_game_players(self.game_id)
        alive = [p for p in players if p["alive"] and not p["eliminated"]]

        if not alive:
            await interaction.response.send_message("생존자가 없습니다.", ephemeral=True)
            return

        lines = []
        for i, p in enumerate(alive[:15], 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
            lines.append(f"{medal} <@{p['user_id']}> — **{p['money']:,}** 코인")

        embed = discord.Embed(
            title="🏆 현재 생존자 랭킹",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# 슬래시 커맨드
# ============================================================
@bot.tree.command(name="테스트게임", description="혼자서 바로 테스트할 수 있는 게임을 시작합니다 (최소인원 무시)")
async def test_game(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    # 이미 진행 중인 게임이 있으면 안내
    playing = await get_playing_game(interaction.channel.id)
    if playing:
        await interaction.response.send_message(
            "이미 이 채널에서 진행 중인 게임이 있습니다.\n"
            "`/강제종료` 후 다시 시도해주세요.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)

    try:
        # 플레이어 생성
        await get_or_create_player(interaction.user, interaction.guild)

        connection = await get_db()
        try:
            async with connection.transaction():
                # 테스트 게임 생성
                game = await connection.fetchrow(
                    """
                    INSERT INTO games (
                        game_type, status, host_id, channel_id,
                        current_phase, started_at, game_data
                    )
                    VALUES (
                        'money_battle_royale_test',
                        'playing',
                        $1, $2, 'survival_test', NOW(),
                        $3::jsonb
                    )
                    RETURNING *
                    """,
                    str(interaction.user.id),
                    str(interaction.channel.id),
                    '{"test": true, "starting_money": 10000, "min_players": 1, "elimination_interval": 300}'
                )

                # 참가자 등록
                await connection.execute(
                    """
                    INSERT INTO game_players (game_id, user_id, bet_amount, result, profit)
                    VALUES ($1, $2, 0, NULL, 0)
                    ON CONFLICT (game_id, user_id) DO NOTHING
                    """,
                    game["id"], str(interaction.user.id)
                )

                # 플레이어 상태 초기화
                next_elim = datetime.utcnow() + timedelta(seconds=DEFAULT_ELIMINATION_INTERVAL)

                await connection.execute(
                    """
                    UPDATE players
                    SET money = $1 + COALESCE(bonus_starting_money, 0),
                        bonus_starting_money = 0,
                        alive = TRUE,
                        eliminated = FALSE,
                        good_deed = 0,
                        inventory = '{}'::jsonb,
                        angel_item = NULL,
                        last_money_loss = 0,
                        cupid_link_user_id = NULL,
                        updated_at = NOW()
                    WHERE user_id = $2
                    """,
                    STARTING_MONEY, str(interaction.user.id)
                )

                # 다음 탈락 시간 저장
                await connection.execute(
                    """
                    UPDATE games
                    SET game_data = jsonb_set(
                        COALESCE(game_data, '{}'::jsonb),
                        '{next_elimination_at}',
                        to_jsonb($2::text),
                        TRUE
                    )
                    WHERE id = $1
                    """,
                    game["id"], next_elim.isoformat()
                )

        finally:
            await connection.close()

        # 탈락 타이머 시작
        start_elimination_task(game["id"], interaction.channel)

        # 메인 UI 전송
        embed = await build_main_embed(game["id"], interaction.user.id, interaction.guild.id)
        view = SurvivalGameView(game["id"])

        await interaction.followup.send(
            content="🧪 **테스트 게임이 시작되었습니다!**\n(최소 인원 무시 + 혼자 플레이 가능)",
            embed=embed,
            view=view
        )

    except Exception as e:
        print("Test game error:", type(e).__name__, str(e))
        await interaction.followup.send(
            f"🔴 테스트 게임 생성 실패\n`{type(e).__name__}`: {str(e)[:300]}",
            ephemeral=True
        )

@bot.tree.command(name="메인", description="머니 배틀로얄 메인 메뉴")
async def main_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    # 진행 중인 게임이 있는지 확인
    playing = await get_playing_game(interaction.channel.id)
    if playing:
        embed = await build_main_embed(playing["id"], interaction.user.id, interaction.guild.id)
        view = SurvivalGameView(playing["id"])
        await interaction.response.send_message(embed=embed, view=view)
        return

    # 대기 중인 게임 확인
    waiting = await get_waiting_game(interaction.channel.id)
    if waiting:
        count = await get_player_count(waiting["id"])
        embed = discord.Embed(
            title="💰 MONEY BATTLE ROYALE - 대기실",
            description=(
                f"👥 현재 참가자: **{count}명**\n"
                f"🎯 최소 인원: **{MIN_PLAYERS}명**\n\n"
                "참가하거나 방장이 시작을 눌러주세요."
            ),
            color=discord.Color.blue()
        )
        view = WaitingView(waiting["id"])
        await interaction.response.send_message(embed=embed, view=view)
        return

    # 새 게임 생성
    game = await create_waiting_game(interaction.guild, interaction.channel, interaction.user.id)
    await join_game_player(interaction.user, interaction.guild, interaction.channel)

    embed = discord.Embed(
        title="💰 MONEY BATTLE ROYALE - 대기실",
        description=(
            f"🎮 새로운 게임이 개설되었습니다!\n"
            f"👥 현재 참가자: **1명**\n"
            f"🎯 최소 인원: **{MIN_PLAYERS}명**\n\n"
            "참가 버튼을 눌러 참여하세요."
        ),
        color=discord.Color.green()
    )
    view = WaitingView(game["id"])
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="강제종료", description="현재 채널의 게임을 강제 종료합니다 (방장/관리자)")
@app_commands.default_permissions(administrator=True)
async def force_end(interaction: discord.Interaction):
    game = await get_playing_game(interaction.channel.id)
    if not game:
        game = await get_waiting_game(interaction.channel.id)

    if not game:
        await interaction.response.send_message("진행 중인 게임이 없습니다.", ephemeral=True)
        return

    connection = await get_db()
    try:
        await connection.execute(
            "UPDATE games SET status = 'cancelled', current_phase = 'force_ended' WHERE id = $1",
            game["id"]
        )
    finally:
        await connection.close()

    if game["id"] in active_elimination_tasks:
        active_elimination_tasks[game["id"]].cancel()

    await interaction.response.send_message("🛑 게임이 강제 종료되었습니다.")


@bot.tree.command(name="다이아상점", description="다이아로 시작 자금을 강화합니다 (게임 시작 전 전용)")
async def diamond_shop(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    # 진행 중인 게임이 있으면 차단
    playing = await get_playing_game(interaction.channel.id)
    if playing:
        await interaction.response.send_message(
            "🔒 **게임이 이미 시작된 후에는 다이아 상점을 이용할 수 없습니다.**\n"
            "다음 서바이벌이 시작되기 전에 `/다이아상점` 을 이용해주세요.",
            ephemeral=True
        )
        return

    player = await get_or_create_player(interaction.user, interaction.guild)
    bonus = player.get("bonus_starting_money") or 0

    embed = discord.Embed(
        title="💎 다이아 상점",
        description=(
            "게임 **시작 전**에만 이용 가능합니다.\n"
            "다이아를 사용해 이번 서바이벌의 **시작 자금**을 강화하세요.\n\n"
            f"💎 보유 다이아: **{player['diamonds']}**\n"
            f"🪙 현재 누적 시작 보너스: **+{bonus:,}** 코인\n\n"
            "구매한 보너스는 게임 시작 시 기본 10,000에 더해지며,\n"
            "적용 후 보너스는 0으로 초기화됩니다."
        ),
        color=discord.Color.purple()
    )
    for key, item in DIAMOND_SHOP_ITEMS.items():
        embed.add_field(
            name=f"{item['emoji']} {item['desc']}",
            value=f"💎 **{item['price']}** 다이아 → +**{item['bonus_money']:,}** 코인",
            inline=False
        )
    embed.set_footer(text="게임 시작 후에는 이용 불가")

    await interaction.response.send_message(
        embed=embed, view=DiamondShopView(), ephemeral=True
    )


@bot.tree.command(name="설명서", description="머니 배틀로얄 전체 규칙 / 게임 / 아이템 / 알바 상세 설명서")
async def manual_command(interaction: discord.Interaction):
    """매우 자세한 통합 설명서"""
    embeds = []

    # 1. 기본 규칙
    e1 = discord.Embed(
        title="📖 머니 배틀로얄 설명서 - 기본 규칙",
        description=(
            "**💰 머니 배틀로얄**은 Discord에서 즐기는 가상 재화 기반 멀티 서바이벌 게임입니다.\n\n"
            "돈을 벌고 → 게임을 선택하고 → 아이템을 사고 → 다른 플레이어와 경쟁하고 → 탈락을 피하고 → **최후의 1인**이 되는 것이 목표입니다.\n\n"
            "실제 현금은 사용하지 않으며 모든 경제 활동은 봇 내부 가상 재화로만 이루어집니다.\n"
            f"**최소 플레이어: {MIN_PLAYERS}명**"
        ),
        color=discord.Color.gold()
    )
    e1.add_field(
        name="🏆 게임 시작 방법",
        value=(
            "`/메인` 명령어로 대기실을 열거나 참가합니다.\n"
            "방장이 설정을 완료한 뒤 **시작** 버튼을 누르면 3초 후 게임이 시작됩니다.\n"
            f"기본 시작 자금: **{STARTING_MONEY:,}** 코인"
        ),
        inline=False
    )
    e1.add_field(
        name="☠️ 탈락 시스템",
        value=(
            "설정된 주기마다 **보유 코인이 가장 적은 플레이어**가 탈락합니다.\n"
            "기본: 5분마다 1명 탈락 (방장이 변경 가능)\n"
            "동점 시 랜덤 처리\n"
            "탈락자의 코인은 잭팟으로 이동합니다.\n"
            "마지막 1명이 우승하며 다이아 등 보상을 받습니다.\n"
            "게임 종료 후 코인과 선행 포인트는 초기화됩니다."
        ),
        inline=False
    )
    e1.add_field(
        name="🪙 재화 설명",
        value=(
            "**코인**: 이번 서바이벌에서만 사용하는 핵심 재화. 게임/알바/이벤트로 획득, 배팅/상점에 사용.\n"
            "**다이아**: 게임을 넘어 유지되는 장기 재화. 출석/우승 등으로 획득. `/다이아상점`에서 시작 자금 강화에 사용.\n"
            "**선행 포인트**: 기부하면 획득. 천사의 상점에서 사용. 게임 종료 시 사라짐."
        ),
        inline=False
    )
    embeds.append(e1)

    # 2. 기부 & 천사의 상점
    e2 = discord.Embed(title="📖 기부 & 천사의 상점", color=discord.Color.purple())
    e2.add_field(
        name="😇 기부 시스템",
        value=(
            "현재 **꼴등**에게 코인을 기부할 수 있습니다.\n"
            "기부자 코인 감소 → 수혜자 코인 증가 → 기부자 선행 포인트 증가 (1:1)\n"
            "부자가 꼴등을 살려주는 전략이 가능합니다."
        ),
        inline=False
    )
    e2.add_field(
        name="🪽 천사의 상점 (한 게임당 1개만 구매 가능)",
        value=(
            "🪽 **천사의 구원** (3,000,000P) : 탈락 대상이 되었을 때 1회 생존\n"
            "🏹 **큐피트 소환권** (6,000,000P) : 꼴등과 랜덤한 사람(자기 포함)을 연결. 꼴등 탈락 시 연결된 사람도 탈락\n"
            "⏳ **신의 모래시계** (7,000,000P) : 방금 전 잃은 돈을 1회 복구\n"
            "🕊️ **천사의 구제** (8,000,000P) : 1~3등 재산 30%를 4등 이하에게 분배 (소수 인원 시 규칙 변경)\n"
            "🏛️ **승천궁** (10,000,000P) : 최고급 아이템 (탈락자 임시 부활 등 복잡한 효과)"
        ),
        inline=False
    )
    embeds.append(e2)

    # 3. 일반 상점 & 아이템
    e3 = discord.Embed(title="📖 일반 상점 & 아이템", color=discord.Color.blue())
    e3.add_field(
        name="🏪 일반 상점 아이템",
        value=(
            "👁️ **정찰권** 300,000 : 상대/현재 순위 정보 확인\n"
            "🪣 **빨대 쪼옵** 600,000 : 원하는 상대의 돈을 랜덤 소액 가져옴\n"
            "🎟️ **이벤트 참가권** 500,000 : 보물찾기 등 이벤트 참가\n"
            "⏳ **시간 연장권** 1,000,000 : 다음 탈락 판정 3분 연장\n"
            "🎁 **랜덤박스** 400,000~ : 개봉 시 코인/다이아/아이템/꽝\n"
            "🎭 **밑장빼기권** 800,000 : 카드 게임 시작 전 패 하나 랜덤 교체\n"
            "🔨 **경매 주최권** 1,500,000 : 미스터리 코인 상자 경매 시작"
        ),
        inline=False
    )
    e3.add_field(
        name="🎒 아이템 사용 규칙",
        value=(
            "대부분의 아이템은 **미니게임 시작 전 10초** 동안 사용 여부를 결정합니다.\n"
            "가방(`🎒 가방` 버튼)에서 미리 사용하거나 확인할 수 있습니다.\n"
            "시간 연장권, 빨대 쪼옵, 정찰권, 랜덤박스 등은 가방에서 바로 사용 가능합니다.\n"
            "아이템 사용 사실은 기본적으로 숨겨지며, 효과가 발생하면 결과가 공개될 수 있습니다."
        ),
        inline=False
    )
    embeds.append(e3)

    # 4. 알바
    e4 = discord.Embed(title="📖 알바 시스템", color=discord.Color.green())
    e4.add_field(
        name="🧑‍💼 알바 목록 (참가비 없음, 미니게임으로 코인 획득)",
        value=(
            "🧹 **청소** - 20,000 : 오염물 빠르게 클릭\n"
            "📦 **택배** - 22,000 : 주소 확인 후 상자 분류\n"
            "🎯 **과녁** - 22,000 : 화면에 나오는 과녁 클릭\n"
            "🍔 **패스트푸드** - 25,000 : 주문서 보고 재료 순서 맞추기\n"
            "🏃 **배달** - 25,000 : 주어진 경로 순서대로 방문\n"
            "🍳 **주방** - 28,000 : 레시피 암기 후 요리 제작\n"
            "🧠 **데이터 입력** - 30,000 : 제시된 문자/숫자 정확히 입력\n"
            "🎣 **낚시** - 30,000 : 타이밍 맞춰 낚아채기\n\n"
            "한 판 약 10~30초, 성공도에 따라 보상 증가.\n"
            "무한 파밍 방지를 위해 **5분 쿨타임**이 적용됩니다."
        ),
        inline=False
    )
    embeds.append(e4)

    # 5. 주요 게임 규칙
    e5 = discord.Embed(title="📖 주요 미니게임 규칙 (1)", color=discord.Color.orange())
    e5.add_field(
        name="🃏 블랙잭 / 마스터 블랙잭",
        value="HIT, STAND, DOUBLE, SURRENDER 가능. 마스터 모드는 SPLIT 등 추가 규칙 포함.",
        inline=False
    )
    e5.add_field(
        name="🃏 에이스 브레이커 (핵심 심리전)",
        value=(
            "카드: 2~9, ACE, JOKER (여러 덱 취급)\n"
            "3장씩 들고 높음→낮음→높음 순서로 비교.\n"
            "ACE는 무조건 승리, 단 상대 JOKER가 있으면 ACE 패배.\n"
            "JOKER는 ACE를 막는 용도. 멀리건 1회 가능.\n"
            "배팅은 랜덤 순서, 후공자는 선배팅 이상 금액 제시."
        ),
        inline=False
    )
    e5.add_field(
        name="🎲 미니 친치로",
        value=(
            "주사위 3개. 부모/자식 결정 후 배팅.\n"
            "즉시 승리 조건, 족보(핀조로 10배, 고조로 5배, 아라시 3배, 시고로 2배, 히후미 손실 등) 존재."
        ),
        inline=False
    )
    e5.add_field(
        name="🧠 인디언 포커",
        value="자신의 카드가 자신에게 안 보이고 상대에게만 보임. BET / RAISE / FOLD 심리전.",
        inline=False
    )
    embeds.append(e5)

    e6 = discord.Embed(title="📖 주요 미니게임 규칙 (2)", color=discord.Color.orange())
    e6.add_field(
        name="🎡 룰렛 / 💣 폭탄 룰렛",
        value=(
            "숫자, 홀짝, 색상, 구간 등 다양한 배팅.\n"
            "폭탄 룰렛은 안전한 칸이 점점 줄어들며 배율이 상승하는 고위험 버전."
        ),
        inline=False
    )
    e6.add_field(
        name="🔢 홀짝 / 🎭 야바위 / 🏇 경마",
        value=(
            "홀짝: 최대 7라운드, 맞으면 +0.3배 / 틀리면 -0.3배 후 수수료.\n"
            "야바위: 컵 중 당첨 보상 찾기. 난이도 조절 가능.\n"
            "경마: 말마다 확률/배율 다름. 실시간 진행."
        ),
        inline=False
    )
    e6.add_field(
        name="🎟️ 잭팟 복권 / 🎫 즉석복권 / 🎁 랜덤박스",
        value=(
            "잭팟 복권: 탈락 1분 전 추첨. 미리 번호 구매.\n"
            "즉석복권: 구매 즉시 결과.\n"
            "복권/상점 수익은 JACKPOT 풀로 누적.\n"
            "랜덤박스는 0~3배 보상 가능."
        ),
        inline=False
    )
    embeds.append(e6)

    # 마지막
    e7 = discord.Embed(
        title="📖 기타 / 명령어",
        description=(
            "**주요 명령어**\n"
            "`/메인` - 게임 메인 메뉴 / 참가\n"
            "`/다이아상점` - 시작 전 시작자금 강화 (다이아 사용)\n"
            "`/설명서` - 이 설명서\n"
            "`/강제종료` - 관리자용 강제 종료\n"
            "`/테스트게임` - 혼자 테스트용 즉시 시작\n\n"
            "**중요 알림**은 원래 채널에 공개됩니다 (탈락, 우승, 잭팟, 천사 아이템 등).\n"
            "서버별 옵션으로 일부 시스템을 ON/OFF 할 수 있습니다."
        ),
        color=discord.Color.dark_grey()
    )
    embeds.append(e7)

    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)  # Discord max 10 embeds


# ============================================================
# 봇 이벤트
# ============================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # DB 스키마 보정 (inventory, bonus_starting_money)
    try:
        await init_db()
    except Exception as e:
        print(f"init_db error: {e}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash sync error: {e}")


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    threading.Thread(target=start_web_server, daemon=True).start()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN이 설정되지 않았습니다.")

    bot.run(token)