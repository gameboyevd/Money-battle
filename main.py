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
JOB_COOLDOWN_SECONDS = 5 * 60          # 기본 알바 충전 쿨타임 (초)
DEFAULT_ELIMINATION_INTERVAL = 5 * 60  # 기본 탈락 주기 (초)
MAX_JOB_CHARGES = 3                    # 알바 기회 최대

# user_id -> {"charges": int, "next_charge_at": datetime|None}
job_states = {}
_user_locks = defaultdict(asyncio.Lock)
active_elimination_tasks = {}  # game_id: asyncio.Task

# 도박 중퇴 방지 세션: user_id -> {type, bet, channel_id, game_id, started_at}
active_gambles = {}


def is_user_gambling(user_id: int) -> bool:
    return int(user_id) in active_gambles


def register_gamble(user_id: int, gtype: str, bet: int, channel_id: int, game_id: int):
    active_gambles[int(user_id)] = {
        "type": gtype,
        "bet": bet,
        "channel_id": int(channel_id),
        "game_id": game_id,
        "started_at": datetime.utcnow(),
    }


def clear_gamble(user_id: int):
    active_gambles.pop(int(user_id), None)


# 시간 초과 중퇴 패널티: user_id -> datetime (이 시각까지 도박/알바 불가)
TIMEOUT_PENALTY_SECONDS = 5 * 60  # 기본 5분
timeout_penalties = {}


def apply_timeout_penalty(user_id: int, seconds: int = None):
    sec = seconds if seconds is not None else TIMEOUT_PENALTY_SECONDS
    timeout_penalties[int(user_id)] = datetime.utcnow() + timedelta(seconds=sec)


def get_penalty_remaining(user_id: int) -> int:
    until = timeout_penalties.get(int(user_id))
    if not until:
        return 0
    left = int((until - datetime.utcnow()).total_seconds())
    if left <= 0:
        timeout_penalties.pop(int(user_id), None)
        return 0
    return left


def is_timeout_penalized(user_id: int) -> bool:
    return get_penalty_remaining(user_id) > 0


def action_lock(user_id: int):
    return _user_locks[user_id]


def default_game_settings():
    return {
        "starting_money": STARTING_MONEY,
        "min_players": MIN_PLAYERS,
        "elimination_interval": DEFAULT_ELIMINATION_INTERVAL,
        "job_cooldown": JOB_COOLDOWN_SECONDS,
        "treasure_hunt_enabled": True,
        "timeout_penalty": TIMEOUT_PENALTY_SECONDS,
    }


def merge_game_settings(game_data) -> dict:
    """game_data + 기본 설정 병합 (설정 키 유지)"""
    data = parse_game_data(game_data)
    base = default_game_settings()
    for k in ("elimination_interval", "job_cooldown", "treasure_hunt_enabled",
              "starting_money", "min_players", "next_elimination_at", "ascension",
              "timeout_penalty"):
        if k in data:
            base[k] = data[k]
    for k, v in data.items():
        if k not in base:
            base[k] = v
    return base


async def get_timeout_penalty_seconds(game_id: int) -> int:
    game = await get_game_by_id(game_id)
    if not game:
        return TIMEOUT_PENALTY_SECONDS
    settings = merge_game_settings(game["game_data"])
    return int(settings.get("timeout_penalty", TIMEOUT_PENALTY_SECONDS))


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
            json.dumps(default_game_settings())
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
                    json.dumps(default_game_settings())
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

            # 방 설정 반영
            settings = merge_game_settings(game["game_data"])
            elim_interval = int(settings.get("elimination_interval", DEFAULT_ELIMINATION_INTERVAL))
            next_elim = datetime.utcnow() + timedelta(seconds=elim_interval)

            # 설정 유지하면서 next_elimination_at만 갱신
            new_data = dict(settings)
            new_data["next_elimination_at"] = next_elim.isoformat()

            await connection.execute(
                """
                UPDATE games
                SET
                    status = 'playing',
                    current_phase = 'survival',
                    started_at = NOW(),
                    game_data = $2::jsonb
                WHERE id = $1 AND status = 'waiting'
                """,
                game_id, json.dumps(new_data)
            )

            players = await connection.fetch(
                "SELECT user_id FROM game_players WHERE game_id = $1", game_id
            )

            for player in players:
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
                # 알바 기회 풀충전 초기화
                try:
                    reset_job_state(int(player["user_id"]))
                except (TypeError, ValueError):
                    reset_job_state(player["user_id"])

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
            game_row = await connection.fetchrow("SELECT game_data FROM games WHERE id = $1", game_id)
            game_data = parse_game_data(game_row["game_data"] if game_row else None)
            ascension = game_data.get("ascension") or {}
            force_ids = set(str(x) for x in (ascension.get("force_elim_next") or []))
            mark_target = str(ascension.get("mark_target_user_id") or "")
            revived_money = int(ascension.get("revived_money") or 0)
            revived_id = str(ascension.get("revived_user_id") or "")

            # 생존자 중 코인 낮은 순으로 조회
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
                return None

            # 승천궁 강제 탈락 대상 우선 포함
            to_eliminate = []
            used_ids = set()
            for p in alive:
                if str(p["user_id"]) in force_ids and str(p["user_id"]) not in used_ids:
                    to_eliminate.append(p)
                    used_ids.add(str(p["user_id"]))
            for p in alive:
                if len(to_eliminate) >= count:
                    break
                if str(p["user_id"]) not in used_ids:
                    to_eliminate.append(p)
                    used_ids.add(str(p["user_id"]))

            eliminated_list = []
            saved_by_angel = []
            ascension_penalty = []  # (user_id, amount)

            # 1차: 천사의 구원 체크 (강제 탈락 대상은 구원 불가)
            final_targets = []
            for player in to_eliminate:
                uid = str(player["user_id"])
                if player["angel_item"] == "천사의 구원" and uid not in force_ids:
                    await connection.execute(
                        "UPDATE players SET angel_item = NULL, updated_at = NOW() WHERE user_id = $1",
                        uid
                    )
                    saved_by_angel.append(player)
                else:
                    final_targets.append(player)

            # 2차: 실제 탈락 + 큐피트 연쇄 + 승천궁 표식
            extra_from_cupid = []
            eliminated_ids = set()
            for player in final_targets:
                linked_id = player.get("cupid_link_user_id")
                uid = str(player["user_id"])

                await connection.execute(
                    """
                    UPDATE players
                    SET alive = FALSE, eliminated = TRUE, cupid_link_user_id = NULL, updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    uid
                )
                eliminated_list.append(player)
                eliminated_ids.add(uid)

                if linked_id:
                    linked_player = await connection.fetchrow(
                        """
                        SELECT user_id, username, money FROM players
                        WHERE user_id = $1 AND alive = TRUE AND eliminated = FALSE
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
                        eliminated_ids.add(str(linked_id))

            # 승천궁: 표식 대상이 이번 탈락에 포함되면 코인 차감
            if mark_target and mark_target in eliminated_ids and revived_money > 0:
                cur = await connection.fetchval(
                    "SELECT money FROM players WHERE user_id = $1", mark_target
                )
                # 이미 탈락 처리됨 — 보유 코인에서 차감(음수 방지). 탈락자라 코인은 의미 없지만 기록용
                deduct = min(revived_money, max(0, cur or 0))
                if deduct > 0:
                    await connection.execute(
                        "UPDATE players SET money = GREATEST(0, money - $1), updated_at = NOW() WHERE user_id = $2",
                        deduct, mark_target
                    )
                else:
                    # 탈락자 코인이 0이면 차감 금액을 알림용으로만
                    deduct = revived_money
                ascension_penalty.append({"user_id": mark_target, "amount": deduct})

            # ascension 데이터 클리어
            if ascension:
                new_data = dict(game_data)
                new_data.pop("ascension", None)
                await connection.execute(
                    "UPDATE games SET game_data = $1::jsonb WHERE id = $2",
                    json.dumps(new_data), game_id
                )

            return {
                "eliminated": eliminated_list,
                "saved": saved_by_angel,
                "cupid_extra": extra_from_cupid,
                "ascension_penalty": ascension_penalty,
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

                    # 승천궁 표식 페널티
                    for pen in result.get("ascension_penalty", []):
                        embed = discord.Embed(
                            title="🏛️ 승천궁 표식 발동!",
                            description=(
                                f"<@{pen['user_id']}> 님이 승천궁 표식 대상이었고 이번 탈락에 포함되어\n"
                                f"**{pen['amount']:,}** 코인이 차감되었습니다."
                            ),
                            color=discord.Color.dark_gold()
                        )
                        await channel.send(embed=embed)

                # 게임 종료 체크
                ended = await check_and_end_game(game_id, channel)
                if ended:
                    break

                # 다음 탈락 시간 갱신 (방 설정 주기 사용)
                connection = await get_db()
                try:
                    g = await connection.fetchrow("SELECT game_data FROM games WHERE id = $1", game_id)
                    settings = merge_game_settings(g["game_data"] if g else None)
                    interval = int(settings.get("elimination_interval", DEFAULT_ELIMINATION_INTERVAL))
                    new_next = datetime.utcnow() + timedelta(seconds=interval)
                    new_data = dict(settings)
                    new_data["next_elimination_at"] = new_next.isoformat()
                    await connection.execute(
                        "UPDATE games SET game_data = $2::jsonb WHERE id = $1",
                        game_id, json.dumps(new_data)
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

class RoomSettingsModal(discord.ui.Modal, title="⚙️ 방 설정 변경"):
    def __init__(self, game_id, settings: dict):
        super().__init__()
        self.game_id = game_id
        self.elim = discord.ui.TextInput(
            label="탈락 주기 (분, 1~60)",
            placeholder="예: 5",
            default=str(max(1, int(settings.get("elimination_interval", 300)) // 60)),
            min_length=1,
            max_length=2,
            required=True
        )
        self.job_cd = discord.ui.TextInput(
            label="알바 기회 충전 쿨타임 (분, 1~30)",
            placeholder="예: 5",
            default=str(max(1, int(settings.get("job_cooldown", 300)) // 60)),
            min_length=1,
            max_length=2,
            required=True
        )
        self.treasure = discord.ui.TextInput(
            label="보물찾기 이벤트 (on/off)",
            placeholder="on 또는 off",
            default="on" if settings.get("treasure_hunt_enabled", True) else "off",
            min_length=2,
            max_length=3,
            required=True
        )
        self.penalty = discord.ui.TextInput(
            label="중퇴 패널티 시간 (분, 1~30)",
            placeholder="예: 5",
            default=str(max(1, int(settings.get("timeout_penalty", 300)) // 60)),
            min_length=1,
            max_length=2,
            required=True
        )
        self.add_item(self.elim)
        self.add_item(self.job_cd)
        self.add_item(self.treasure)
        self.add_item(self.penalty)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            elim_m = int(self.elim.value.strip())
            job_m = int(self.job_cd.value.strip())
            pen_m = int(self.penalty.value.strip())
            if not (1 <= elim_m <= 60):
                await interaction.response.send_message("탈락 주기는 1~60분이어야 합니다.", ephemeral=True)
                return
            if not (1 <= job_m <= 30):
                await interaction.response.send_message("알바 쿨타임은 1~30분이어야 합니다.", ephemeral=True)
                return
            if not (1 <= pen_m <= 30):
                await interaction.response.send_message("패널티는 1~30분이어야 합니다.", ephemeral=True)
                return
            th_raw = self.treasure.value.strip().lower()
            if th_raw not in ("on", "off", "온", "오프", "o", "x"):
                await interaction.response.send_message("보물찾기는 on/off 로 입력하세요.", ephemeral=True)
                return
            th = th_raw in ("on", "온", "o")
        except ValueError:
            await interaction.response.send_message("숫자를 올바르게 입력하세요.", ephemeral=True)
            return

        connection = await get_db()
        try:
            game = await connection.fetchrow(
                "SELECT * FROM games WHERE id = $1 AND status = 'waiting'", self.game_id
            )
            if not game:
                await interaction.response.send_message("대기 중인 게임이 없습니다.", ephemeral=True)
                return
            if str(game["host_id"]) != str(interaction.user.id):
                await interaction.response.send_message("방장만 변경할 수 있습니다.", ephemeral=True)
                return
            data = merge_game_settings(game["game_data"])
            data["elimination_interval"] = elim_m * 60
            data["job_cooldown"] = job_m * 60
            data["treasure_hunt_enabled"] = th
            data["timeout_penalty"] = pen_m * 60
            await connection.execute(
                "UPDATE games SET game_data = $1::jsonb WHERE id = $2",
                json.dumps(data), self.game_id
            )
        finally:
            await connection.close()

        th_txt = "ON ✅" if th else "OFF ❌"
        await interaction.response.send_message(
            f"⚙️ 설정 저장 완료!\n"
            f"⏰ 탈락 주기: **{elim_m}분**\n"
            f"🧑‍💼 알바 충전 쿨: **{job_m}분**\n"
            f"🗺️ 보물찾기: **{th_txt}**\n"
            f"🚫 중퇴 패널티: **{pen_m}분**",
            ephemeral=True
        )


class RoomSettingsView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=120)
        self.game_id = game_id

    @discord.ui.button(label="설정 변경", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = await get_game_by_id(self.game_id)
        if not game or game["status"] != "waiting":
            await interaction.response.send_message("대기 중이 아닙니다.", ephemeral=True)
            return
        if str(game["host_id"]) != str(interaction.user.id):
            await interaction.response.send_message("방장만 가능합니다.", ephemeral=True)
            return
        settings = merge_game_settings(game["game_data"])
        await interaction.response.send_modal(RoomSettingsModal(self.game_id, settings))

    @discord.ui.button(label="보물찾기 토글", emoji="🗺️", style=discord.ButtonStyle.secondary)
    async def toggle_treasure(self, interaction: discord.Interaction, button: discord.ui.Button):
        connection = await get_db()
        try:
            game = await connection.fetchrow(
                "SELECT * FROM games WHERE id = $1 AND status = 'waiting'", self.game_id
            )
            if not game:
                await interaction.response.send_message("대기 중이 아닙니다.", ephemeral=True)
                return
            if str(game["host_id"]) != str(interaction.user.id):
                await interaction.response.send_message("방장만 가능합니다.", ephemeral=True)
                return
            data = merge_game_settings(game["game_data"])
            data["treasure_hunt_enabled"] = not data.get("treasure_hunt_enabled", True)
            await connection.execute(
                "UPDATE games SET game_data = $1::jsonb WHERE id = $2",
                json.dumps(data), self.game_id
            )
            th = "ON ✅" if data["treasure_hunt_enabled"] else "OFF ❌"
        finally:
            await connection.close()
        await interaction.response.send_message(f"🗺️ 보물찾기 이벤트: **{th}**", ephemeral=True)


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

    @discord.ui.button(label="방 설정", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = await get_waiting_game(interaction.channel.id)
        if not game or game["id"] != self.game_id:
            await interaction.response.send_message("대기 중인 게임이 없습니다.", ephemeral=True)
            return
        if str(game["host_id"]) != str(interaction.user.id):
            await interaction.response.send_message("🔒 방장만 설정을 변경할 수 있습니다.", ephemeral=True)
            return
        settings = merge_game_settings(game["game_data"])
        elim_m = int(settings["elimination_interval"]) // 60
        job_m = int(settings["job_cooldown"]) // 60
        pen_m = int(settings.get("timeout_penalty", TIMEOUT_PENALTY_SECONDS)) // 60
        th = "✅ ON" if settings.get("treasure_hunt_enabled", True) else "❌ OFF"
        embed = discord.Embed(
            title="⚙️ 방 설정",
            description=(
                f"⏰ 탈락 주기: **{elim_m}분**\n"
                f"🧑‍💼 알바 충전 쿨타임: **{job_m}분**\n"
                f"🚫 중퇴 패널티: **{pen_m}분**\n"
                f"🗺️ 보물찾기 이벤트: **{th}**\n\n"
                f"💎 개인 추가자금은 `/다이아상점`에서 각자 구매\n"
                f"(기본 시작자금 {STARTING_MONEY:,} + 보너스)"
            ),
            color=discord.Color.blue()
        )
        await interaction.response.send_message(
            embed=embed, view=RoomSettingsView(self.game_id), ephemeral=True
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


def format_seconds(seconds: int):
    minutes = seconds // 60
    seconds %= 60
    if minutes > 0:
        return f"{minutes}분 {seconds}초"
    return f"{seconds}초"


def _get_job_state(user_id: int) -> dict:
    st = job_states.get(user_id)
    if st is None:
        st = {"charges": MAX_JOB_CHARGES, "next_charge_at": None}
        job_states[user_id] = st
    return st


def refresh_job_charges(user_id: int, cooldown_seconds: int = None) -> dict:
    """쿨타임이 지났으면 기회 +1 (최대 MAX_JOB_CHARGES). 여러 번 충전 가능."""
    if cooldown_seconds is None:
        cooldown_seconds = JOB_COOLDOWN_SECONDS
    st = _get_job_state(user_id)
    now = datetime.utcnow()
    while st["charges"] < MAX_JOB_CHARGES and st.get("next_charge_at"):
        if now >= st["next_charge_at"]:
            st["charges"] += 1
            if st["charges"] >= MAX_JOB_CHARGES:
                st["next_charge_at"] = None
            else:
                st["next_charge_at"] = st["next_charge_at"] + timedelta(seconds=cooldown_seconds)
                # 이미 더 지났으면 루프로 추가 충전
        else:
            break
    return st


def get_job_charges(user_id: int, cooldown_seconds: int = None) -> tuple:
    """(charges, seconds_until_next_charge) — 3이면 remaining=0"""
    st = refresh_job_charges(user_id, cooldown_seconds)
    if st["charges"] >= MAX_JOB_CHARGES or not st.get("next_charge_at"):
        return st["charges"], 0
    remaining = int((st["next_charge_at"] - datetime.utcnow()).total_seconds())
    return st["charges"], max(0, remaining)


def consume_job_charge(user_id: int, cooldown_seconds: int = None) -> bool:
    """기회 1개 소모. 성공 시 True. 3→2가 되면 쿨타임 시작."""
    if cooldown_seconds is None:
        cooldown_seconds = JOB_COOLDOWN_SECONDS
    st = refresh_job_charges(user_id, cooldown_seconds)
    if st["charges"] <= 0:
        return False
    st["charges"] -= 1
    # 기회가 최대 미만이면 충전 타이머 가동 (이미 돌고 있으면 유지)
    if st["charges"] < MAX_JOB_CHARGES:
        if not st.get("next_charge_at") or st["next_charge_at"] <= datetime.utcnow():
            st["next_charge_at"] = datetime.utcnow() + timedelta(seconds=cooldown_seconds)
    return True


def reset_job_state(user_id: int):
    job_states[user_id] = {"charges": MAX_JOB_CHARGES, "next_charge_at": None}


async def get_job_cooldown_for_game(game_id: int) -> int:
    game = await get_game_by_id(game_id)
    if not game:
        return JOB_COOLDOWN_SECONDS
    settings = merge_game_settings(game["game_data"])
    return int(settings.get("job_cooldown", JOB_COOLDOWN_SECONDS))


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
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "청소")
            await interaction.followup.send(
                f"🧹 **청소 알바 성공!**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n",
                ephemeral=True
            )
        else:
            reward = job["base"] // 3
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
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "과녁")
            msg = f"🎯 **과녁 알바 완벽!**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n"
        else:
            reward = job["base"] // 2 + self.hits * 2_000
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
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, "낚시")
            await interaction.response.edit_message(
                content=f"🎣 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n",
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
        new_money = await give_job_reward(interaction.user, interaction.guild, reward, "데이터 입력")
        await interaction.response.send_message(
            f"🧠 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n",
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
        new_money = await give_job_reward(interaction.user, interaction.guild, reward, "패스트푸드")
        try:
            await interaction.response.edit_message(
                content=f"🍔 **{grade}**\n💰 +**{reward:,}** 코인\n🪙 현재: **{new_money:,}**\n",
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
            new_money = await give_job_reward(interaction.user, interaction.guild, reward, self.job_name)
            await interaction.response.edit_message(
                content=(
                    f"{job['emoji']} **{self.job_name} 알바 완료!**\n"
                    f"💰 +**{reward:,}** 코인\n"
                    f"🪙 현재: **{new_money:,}**\n"
                    f""
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
        pen = get_penalty_remaining(user_id)
        if pen > 0:
            await interaction.response.send_message(
                f"🚫 시간 초과 패널티 중입니다.\n"
                f"도박·알바 이용 불가 — 남은 시간: **{format_seconds(pen)}**",
                ephemeral=True
            )
            return
        cd = await get_job_cooldown_for_game(self.game_id)
        charges, remaining = get_job_charges(user_id, cd)
        if charges <= 0:
            await interaction.response.send_message(
                f"⏳ 알바 기회가 없습니다. (0/{MAX_JOB_CHARGES})\n"
                f"다음 기회까지: **{format_seconds(remaining)}**",
                ephemeral=True
            )
            return
        if not consume_job_charge(user_id, cd):
            await interaction.response.send_message("알바 기회 소모 실패. 다시 시도하세요.", ephemeral=True)
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
            elif item["effect"] == "ascension_palace":
                public_embed = discord.Embed(
                    title="😇 천사의 상점 구매!",
                    description=(
                        f"<@{user_id}> 님이 **🏛️ 승천궁** 을(를) 구매했습니다!\n"
                        f"선행 포인트 -**{item['price']:,}P**"
                    ),
                    color=discord.Color.purple()
                )
                await interaction.channel.send(embed=public_embed)
                await interaction.response.send_message(
                    f"🏛️ **승천궁** 구매 완료!\n"
                    f"😇 -**{item['price']:,}** 선행 포인트\n"
                    f"😇 남은 선행 포인트: **{new_good_deed:,}P**\n\n"
                    f"{item['desc']}",
                    ephemeral=True
                )
                await self._activate_ascension(interaction)
                return
            # 천사의 구원: 탈락 시점에 자동 발동

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

    async def _activate_ascension(self, interaction) -> str:
        """승천궁: 탈락자 1명 임시 부활 → 다음 판정 강제 탈락 → 표식 대상 지정"""
        connection = await get_db()
        try:
            eliminated = await connection.fetch(
                """
                SELECT p.user_id, p.username, p.money
                FROM game_players gp
                JOIN players p ON p.user_id = gp.user_id
                WHERE gp.game_id = $1 AND (p.alive = FALSE OR p.eliminated = TRUE)
                ORDER BY p.username
                LIMIT 25
                """,
                self.game_id
            )
        finally:
            await connection.close()

        if not eliminated:
            return "⚠️ 부활시킬 탈락자가 없습니다. (효과는 구매만 완료됨)"

        view = AscensionReviveView(self.game_id, str(interaction.user.id), eliminated)
        await interaction.followup.send(
            "🏛️ **승천궁** — 임시 부활시킬 탈락자를 선택하세요.\n"
            "(부활자는 다음 탈락 판정에서 **강제 탈락**되며, 생존자 1명을 표식할 수 있습니다.)",
            view=view,
            ephemeral=True
        )
        return "부활 대상 선택 UI를 열었습니다."


class AscensionReviveView(discord.ui.View):
    def __init__(self, game_id, buyer_id, eliminated_rows):
        super().__init__(timeout=90)
        self.game_id = game_id
        self.buyer_id = buyer_id
        options = []
        for p in eliminated_rows[:25]:
            options.append(discord.SelectOption(
                label=f"{p['username'] or p['user_id']}",
                value=str(p["user_id"]),
                description=f"최종 코인 {p['money']:,}"
            ))
        self.select = discord.ui.Select(placeholder="부활시킬 탈락자 선택", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.buyer_id):
            await interaction.response.send_message("구매자만 선택할 수 있습니다.", ephemeral=True)
            return
        revived_id = self.select.values[0]

        connection = await get_db()
        try:
            # 임시 부활
            row = await connection.fetchrow(
                """
                UPDATE players
                SET alive = TRUE, eliminated = FALSE, updated_at = NOW()
                WHERE user_id = $1
                RETURNING user_id, username, money
                """,
                revived_id
            )
            if not row:
                await interaction.response.send_message("부활 실패", ephemeral=True)
                return
            revived_money = row["money"] or 0

            # game_data에 승천궁 정보 저장
            game = await connection.fetchrow("SELECT game_data FROM games WHERE id = $1", self.game_id)
            gdata = parse_game_data(game["game_data"] if game else None)
            gdata["ascension"] = {
                "revived_user_id": revived_id,
                "revived_money": revived_money,
                "force_elim_next": [revived_id],
                "mark_target_user_id": None,
                "buyer_id": self.buyer_id,
            }
            await connection.execute(
                "UPDATE games SET game_data = $1::jsonb WHERE id = $2",
                json.dumps(gdata), self.game_id
            )
        finally:
            await connection.close()

        # 부활자에게 표식 대상 선택 요청 (채널 공개 + 부활자용 버튼)
        public = discord.Embed(
            title="🏛️ 승천궁 발동!",
            description=(
                f"<@{revived_id}> 님이 **임시 부활**했습니다!\n"
                f"부활 시점 코인: **{revived_money:,}**\n\n"
                f"⚠️ 다음 탈락 판정에서 **강제 탈락**됩니다.\n"
                f"부활자는 생존자 1명을 표식할 수 있습니다.\n"
                f"(표식 대상이 다음 판정에 탈락하면 부활 시점 코인만큼 차감)"
            ),
            color=discord.Color.dark_gold()
        )
        await interaction.channel.send(embed=public)

        players = await get_game_players(self.game_id)
        alive = [p for p in players if p["alive"] and not p["eliminated"] and str(p["user_id"]) != revived_id]
        mark_view = AscensionMarkView(self.game_id, revived_id, revived_money, alive)
        await interaction.response.edit_message(
            content=f"🏛️ <@{revived_id}> 부활 완료! 표식 대상 선택 UI가 열렸습니다.",
            view=None
        )
        await interaction.channel.send(
            content=f"<@{revived_id}> 님, 표식할 생존자를 선택하세요!",
            view=mark_view
        )


class AscensionMarkView(discord.ui.View):
    def __init__(self, game_id, revived_id, revived_money, alive_players):
        super().__init__(timeout=120)
        self.game_id = game_id
        self.revived_id = str(revived_id)
        self.revived_money = revived_money
        options = []
        for p in alive_players[:25]:
            options.append(discord.SelectOption(
                label=f"{p['username'] or p['user_id']} — {p['money']:,}",
                value=str(p["user_id"])
            ))
        if not options:
            options.append(discord.SelectOption(label="대상 없음", value="none"))
        self.select = discord.ui.Select(placeholder="표식할 생존자 선택", options=options)
        self.select.callback = self.on_mark
        self.add_item(self.select)

    async def on_mark(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.revived_id:
            await interaction.response.send_message("부활자만 표식할 수 있습니다.", ephemeral=True)
            return
        target = self.select.values[0]
        if target == "none":
            await interaction.response.send_message("표식할 대상이 없습니다.", ephemeral=True)
            return

        connection = await get_db()
        try:
            game = await connection.fetchrow("SELECT game_data FROM games WHERE id = $1", self.game_id)
            gdata = parse_game_data(game["game_data"] if game else None)
            asc = gdata.get("ascension") or {}
            asc["mark_target_user_id"] = target
            gdata["ascension"] = asc
            await connection.execute(
                "UPDATE games SET game_data = $1::jsonb WHERE id = $2",
                json.dumps(gdata), self.game_id
            )
        finally:
            await connection.close()

        embed = discord.Embed(
            title="🏛️ 승천궁 표식!",
            description=(
                f"<@{self.revived_id}> 님이 <@{target}> 님을 표식했습니다!\n"
                f"다음 탈락 판정에 표식 대상이 탈락하면 **{self.revived_money:,}** 코인 차감."
            ),
            color=discord.Color.dark_gold()
        )
        await interaction.channel.send(embed=embed)
        await interaction.response.edit_message(content=f"표식 완료: <@{target}>", view=None)
        self.stop()


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
            players = await get_game_players(game_id)
            alive = sorted(
                [p for p in players if p["alive"] and not p["eliminated"]],
                key=lambda p: p["money"],
                reverse=True
            )
            if not alive:
                return "생존자가 없습니다."
            top_lines = []
            for i, p in enumerate(alive[:3], 1):
                top_lines.append(f"{i}등 <@{p['user_id']}> — **{p['money']:,}**")
            bot_lines = []
            for i, p in enumerate(alive[-3:][::-1] if len(alive) > 3 else alive[::-1], 1):
                bot_lines.append(f"하위 {i} <@{p['user_id']}> — **{p['money']:,}**")
            my_rank = next((i for i, p in enumerate(alive, 1) if str(p["user_id"]) == user_id), None)
            return (
                f"👁️ **정찰 결과** (생존 {len(alive)}명)\n\n"
                f"**상위**\n" + "\n".join(top_lines) + "\n\n"
                f"**하위**\n" + "\n".join(bot_lines) + "\n\n"
                f"내 순위: **{my_rank}위**" if my_rank else "내 순위: 탈락/미참가"
            )

        elif item_name == "빨대 쪼옵":
            # 대상 선택 UI로 넘김 (아이템은 on_select에서 소모하므로 여기서 선택 후 소모 처리)
            players = await get_game_players(game_id)
            others = [p for p in players if p["alive"] and not p["eliminated"] and str(p["user_id"]) != user_id]
            if not others:
                return "가져올 대상이 없습니다."
            # 대상 선택 뷰
            view = StrawSelectView(game_id, user_id, guild_id, others)
            await interaction.response.send_message(
                "🪣 **빨대 쪼옵** — 돈을 가져올 대상을 선택하세요.",
                view=view,
                ephemeral=True
            )
            return None  # 이미 response 함 (소모는 StrawSelectView에서)

        elif item_name == "랜덤박스":
            roll = random.random()
            connection = await get_db()
            try:
                if roll < 0.03:
                    # 다이아
                    dia = random.randint(1, 5)
                    await connection.execute(
                        "UPDATE players SET diamonds = diamonds + $1 WHERE user_id = $2",
                        dia, user_id
                    )
                    msg = f"💎 대박! 다이아 **+{dia}** 획득!"
                elif roll < 0.08:
                    # 아이템
                    gift = random.choice(["정찰권", "시간 연장권", "빨대 쪼옵"])
                    await add_item_to_inventory(user_id, guild_id, gift, 1)
                    msg = f"🎁 아이템 획득! **{gift}** ×1"
                elif roll < 0.20:
                    reward = random.randint(800_000, 1_200_000)
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2", reward, user_id
                    )
                    msg = f"🎉 대박! **{reward:,}** 코인 획득! (약 2~3배)"
                elif roll < 0.45:
                    reward = random.randint(300_000, 600_000)
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2", reward, user_id
                    )
                    msg = f"✨ 성공! **{reward:,}** 코인 획득!"
                elif roll < 0.70:
                    reward = random.randint(100_000, 250_000)
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2", reward, user_id
                    )
                    msg = f"보통... **{reward:,}** 코인 획득"
                elif roll < 0.88:
                    reward = random.randint(10_000, 80_000)
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2", reward, user_id
                    )
                    msg = f"아쉬움... **{reward:,}** 코인"
                else:
                    msg = "💥 꽝... 아무것도 없습니다."
            finally:
                await connection.close()
            return msg

        elif item_name == "밑장빼기권":
            await interaction.response.send_message(
                "🎭 **밑장빼기권**\n"
                "이 아이템은 **블랙잭 / 에이스 브레이커**를 시작할 때 자동으로 사용 여부를 묻습니다.\n"
                "가방에 보유만 해두세요. (지금은 소모되지 않습니다)",
                ephemeral=True
            )
            return None

        elif item_name == "이벤트 참가권":
            game = await get_game_by_id(game_id)
            settings = merge_game_settings(game["game_data"] if game else None)
            if not settings.get("treasure_hunt_enabled", True):
                await interaction.response.send_message(
                    "🗺️ 이 방에서는 **보물찾기 이벤트가 OFF** 입니다.\n(방장이 시작 전 설정에서 켤 수 있습니다)",
                    ephemeral=True
                )
                return None
            view = TreasureHuntView(game_id, user_id, guild_id)
            await interaction.response.send_message(
                "🗺️ **보물찾기 이벤트!**\n"
                "장소를 하나 선택하세요. (성공도에 따라 보상/꽝)\n"
                "참가권 1장이 소모됩니다.",
                view=view,
                ephemeral=True
            )
            return None

        elif item_name == "경매 주최권":
            ch_id = interaction.channel.id
            if active_auctions.get(ch_id):
                await interaction.response.send_message(
                    "이미 이 채널에서 경매가 진행 중입니다.", ephemeral=True
                )
                return None

            embed = discord.Embed(
                title="🚨 SPECIAL EVENT: 미스터리 코인 상자 등장!",
                description=(
                    f"<@{user_id}> 님이 **경매 주최권**을 사용했습니다!\n\n"
                    "🎁 상자 내용물: 🪙 ??? 코인 (주작/초대박/대박/본전/꽝)\n"
                    "🏁 시작가: **100,000** 코인\n"
                    "📈 입찰: +50,000 / +200,000\n"
                    "⏱️ 60초 (입찰 시 5초 연장)\n"
                    f"💰 현재 잭팟: **{mystery_jackpots.get(ch_id, 0):,}**\n\n"
                    "⏳ **10초 후** 경매가 시작됩니다!"
                ),
                color=discord.Color.gold()
            )
            await interaction.channel.send(embed=embed)
            active_auctions[ch_id] = True

            async def _start_auction():
                await asyncio.sleep(10)
                auction_view = MysteryAuctionView(self.game_id, str(user_id), ch_id)
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
# 보물찾기 이벤트
# ============================================================
# 참가: 이벤트 참가권 1장 소모 (또는 코인 200,000 — 티켓 우선)
# 장소별 이벤트가 다르고, 2단계 탐험 가능

TREASURE_LOCATIONS = {
    "forest": {
        "name": "🌲 숲",
        "flavor": ["나무 틈새에서 뭔가가 반짝인다...", "발밑에 오래된 상자가 있다.", "덤불 사이로 금화가 보인다."],
        "events": [
            ("야생동물 습격!", 0.15, "fail", 0),
            ("작은 금화 주머니", 0.25, "coin", (40_000, 100_000)),
            ("보물 상자", 0.20, "coin", (120_000, 350_000)),
            ("희귀 다이아", 0.10, "dia", (1, 2)),
            ("숨겨진 아이템", 0.12, "item", None),
            ("전설의 유물!", 0.08, "coin", (400_000, 1_200_000)),
            ("아무것도 없음", 0.10, "fail", 0),
        ],
    },
    "ruins": {
        "name": "🏚️ 폐허",
        "flavor": ["무너진 기둥 뒤에 금고가...", "먼지 쌓인 상자.", "함정이 있을지도 모른다."],
        "events": [
            ("함정 발동!", 0.18, "fail", 0),
            ("녹슨 동전 더미", 0.22, "coin", (50_000, 130_000)),
            ("고대 유물", 0.18, "coin", (150_000, 400_000)),
            ("다이아 조각", 0.12, "dia", (1, 3)),
            ("봉인된 아이템", 0.12, "item", None),
            ("왕실 금고!", 0.08, "coin", (500_000, 1_500_000)),
            ("빈 방", 0.10, "fail", 0),
        ],
    },
    "castle": {
        "name": "🏰 성",
        "flavor": ["왕좌 아래에 비밀 통로가...", "보물창고 문이 살짝 열려 있다.", "경비는 없는 것 같다."],
        "events": [
            ("경비 출동!", 0.12, "fail", 0),
            ("은화 주머니", 0.20, "coin", (80_000, 180_000)),
            ("왕실 보석함", 0.22, "coin", (200_000, 500_000)),
            ("다이아 왕관 조각", 0.15, "dia", (2, 4)),
            ("귀족의 유품", 0.12, "item", None),
            ("드래곤 골드!", 0.09, "coin", (700_000, 2_000_000)),
            ("빈 금고", 0.10, "fail", 0),
        ],
    },
    "cave": {
        "name": "🕳️ 동굴",
        "flavor": ["어둠 속에서 빛이 보인다...", "종유석 사이에 상자.", "깊은 곳에서 메아리가..."],
        "events": [
            ("낙석!", 0.20, "fail", 0),
            ("광석 덩어리", 0.20, "coin", (60_000, 150_000)),
            ("지하 보물", 0.20, "coin", (180_000, 450_000)),
            ("원석 다이아", 0.12, "dia", (1, 3)),
            ("고대 유물 상자", 0.10, "item", None),
            ("용암 속 금괴!", 0.08, "coin", (600_000, 1_800_000)),
            ("막다른 길", 0.10, "fail", 0),
        ],
    },
}


def _pick_treasure_event(loc_key: str):
    loc = TREASURE_LOCATIONS[loc_key]
    events = loc["events"]
    r = random.random()
    acc = 0.0
    for name, prob, kind, data in events:
        acc += prob
        if r <= acc:
            return name, kind, data
    return events[-1][0], events[-1][2], events[-1][3]


async def _apply_treasure_reward(user_id: str, guild_id: str, kind: str, data) -> str:
    connection = await get_db()
    try:
        if kind == "fail":
            return "💨 꽝... 빈손입니다."
        if kind == "coin":
            lo, hi = data
            reward = random.randint(lo, hi)
            await connection.execute(
                "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2",
                reward, user_id
            )
            return f"🪙 **{reward:,}** 코인 획득!"
        if kind == "dia":
            lo, hi = data
            dia = random.randint(lo, hi)
            await connection.execute(
                "UPDATE players SET diamonds = diamonds + $1, updated_at = NOW() WHERE user_id = $2",
                dia, user_id
            )
            return f"💎 다이아 **+{dia}**"
        if kind == "item":
            gift = random.choice(["정찰권", "랜덤박스", "시간 연장권", "빨대 쪼옵"])
            await add_item_to_inventory(user_id, guild_id, gift, 1)
            return f"🎒 아이템 **{gift}** ×1 획득!"
        return "결과 없음"
    finally:
        await connection.close()


class TreasureHuntView(discord.ui.View):
    """1단계: 장소 선택 → 이벤트 결과 → (성공 시) 더 깊이 탐험 기회"""

    def __init__(self, game_id, user_id, guild_id):
        super().__init__(timeout=90)
        self.game_id = game_id
        self.user_id = str(user_id)
        self.guild_id = str(guild_id)
        self.done = False
        self.ticket_consumed = False
        for key, loc in TREASURE_LOCATIONS.items():
            btn = discord.ui.Button(
                label=loc["name"],
                style=discord.ButtonStyle.primary,
                custom_id=f"th_{key}"
            )
            btn.callback = self._make_loc_cb(key)
            self.add_item(btn)

    def _make_loc_cb(self, loc_key: str):
        async def callback(interaction: discord.Interaction):
            if str(interaction.user.id) != self.user_id:
                await interaction.response.send_message("본인만 탐험할 수 있습니다.", ephemeral=True)
                return
            if self.done:
                await interaction.response.defer()
                return

            # 참가권 소모 (1회)
            if not self.ticket_consumed:
                inv = await get_player_inventory(self.user_id, self.guild_id)
                if inv.get("이벤트 참가권", 0) <= 0:
                    await interaction.response.edit_message(
                        content="🎟️ 이벤트 참가권이 없습니다.", view=None
                    )
                    return
                await remove_item_from_inventory(self.user_id, self.guild_id, "이벤트 참가권", 1)
                self.ticket_consumed = True

            loc = TREASURE_LOCATIONS[loc_key]
            flavor = random.choice(loc["flavor"])
            ev_name, kind, data = _pick_treasure_event(loc_key)
            reward_txt = await _apply_treasure_reward(self.user_id, self.guild_id, kind, data)

            public = discord.Embed(
                title=f"🗺️ 보물찾기 — {loc['name']}",
                description=(
                    f"<@{self.user_id}> 님이 탐험을 떠났습니다!\n\n"
                    f"_{flavor}_\n\n"
                    f"**이벤트:** {ev_name}\n"
                    f"**결과:** {reward_txt}"
                ),
                color=discord.Color.dark_green() if kind != "fail" else discord.Color.dark_grey()
            )
            await interaction.channel.send(embed=public)

            # 성공 시 2단계 심층 탐험 기회 (1회)
            if kind != "fail" and not self.done:
                deep = TreasureDeepView(self.game_id, self.user_id, self.guild_id, loc_key)
                await interaction.response.edit_message(
                    content=(
                        f"{loc['name']} 탐험 결과\n{reward_txt}\n\n"
                        f"🔍 더 깊이 들어갈 수 있습니다. (추가 위험 / 추가 보상)"
                    ),
                    view=deep
                )
            else:
                self.done = True
                self.stop()
                await interaction.response.edit_message(
                    content=f"{loc['name']} 탐험 종료\n{reward_txt}",
                    view=None
                )
        return callback


class TreasureDeepView(discord.ui.View):
    """2단계 심층 탐험 — 성공 시 보상 배율 상승, 실패 확률 증가"""

    def __init__(self, game_id, user_id, guild_id, loc_key):
        super().__init__(timeout=45)
        self.game_id = game_id
        self.user_id = str(user_id)
        self.guild_id = str(guild_id)
        self.loc_key = loc_key

    @discord.ui.button(label="더 깊이 들어간다", emoji="🔦", style=discord.ButtonStyle.danger)
    async def go_deeper(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("본인만 가능합니다.", ephemeral=True)
            return
        self.stop()
        loc = TREASURE_LOCATIONS[self.loc_key]
        # 심층: 실패 확률 상승, 보상 상향
        if random.random() < 0.35:
            result = "💀 심층에서 위험 발생! 빈손으로 탈출했습니다."
            color = discord.Color.red()
        else:
            roll = random.random()
            if roll < 0.15:
                reward = random.randint(800_000, 2_500_000)
                connection = await get_db()
                try:
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2",
                        reward, self.user_id
                    )
                finally:
                    await connection.close()
                result = f"🏆 심층 대보물! **{reward:,}** 코인!"
            elif roll < 0.40:
                reward = random.randint(250_000, 700_000)
                connection = await get_db()
                try:
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2",
                        reward, self.user_id
                    )
                finally:
                    await connection.close()
                result = f"✨ 심층 보물 **{reward:,}** 코인"
            elif roll < 0.55:
                dia = random.randint(2, 5)
                connection = await get_db()
                try:
                    await connection.execute(
                        "UPDATE players SET diamonds = diamonds + $1 WHERE user_id = $2",
                        dia, self.user_id
                    )
                finally:
                    await connection.close()
                result = f"💎 심층 다이아 **+{dia}**"
            else:
                gift = random.choice(["시간 연장권", "정찰권", "랜덤박스", "경매 주최권"])
                await add_item_to_inventory(self.user_id, self.guild_id, gift, 1)
                result = f"🎒 심층 아이템 **{gift}** ×1"
            color = discord.Color.gold()

        embed = discord.Embed(
            title=f"🔦 심층 탐험 — {loc['name']}",
            description=f"<@{self.user_id}>\n\n{result}",
            color=color
        )
        await interaction.channel.send(embed=embed)
        await interaction.response.edit_message(content=result, view=None)

    @discord.ui.button(label="여기서 그만둔다", emoji="🚪", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("본인만 가능합니다.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="탐험을 종료하고 안전하게 돌아왔습니다.", view=None)


# ============================================================
# 빨대 쪼옵 대상 선택
# ============================================================

class StrawSelectView(discord.ui.View):
    def __init__(self, game_id, user_id, guild_id, candidates):
        super().__init__(timeout=60)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        options = []
        for p in candidates[:25]:
            options.append(discord.SelectOption(
                label=f"{p['username'] or p['user_id']} — {p['money']:,}코인",
                value=str(p["user_id"]),
                description=f"보유 {p['money']:,}"
            ))
        self.select = discord.ui.Select(
            placeholder="빨대로 빨아올 대상 선택",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.user_id):
            await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
            return
        target_id = self.select.values[0]
        target = await get_player(target_id, self.guild_id)
        if not target or not target["alive"] or target["eliminated"]:
            await interaction.response.send_message("대상이 이미 탈락했거나 없습니다.", ephemeral=True)
            return
        # 아이템 보유 확인 후 소모
        inv = await get_player_inventory(self.user_id, self.guild_id)
        if inv.get("빨대 쪼옵", 0) <= 0:
            await interaction.response.send_message("빨대 쪼옵을 보유하고 있지 않습니다.", ephemeral=True)
            return
        await remove_item_from_inventory(self.user_id, self.guild_id, "빨대 쪼옵", 1)

        steal = random.randint(10_000, min(100_000, max(10_000, (target["money"] or 0) // 10)))
        if (target["money"] or 0) < steal:
            steal = target["money"] or 0
        if steal <= 0:
            await interaction.response.send_message("상대에게 가져올 돈이 없습니다.", ephemeral=True)
            return

        connection = await get_db()
        try:
            await connection.execute(
                "UPDATE players SET money = money - $1, last_money_loss = $1, updated_at = NOW() WHERE user_id = $2",
                steal, target_id
            )
            new_money = await connection.fetchval(
                "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2 RETURNING money",
                steal, self.user_id
            )
        finally:
            await connection.close()

        embed = discord.Embed(
            title="🪣 빨대 쪼옵!",
            description=f"<@{self.user_id}> 님이 <@{target_id}> 님에게서 **{steal:,}** 코인을 가져갔습니다!",
            color=discord.Color.orange()
        )
        await interaction.channel.send(embed=embed)
        await interaction.response.edit_message(
            content=f"🪣 <@{target_id}> 에게서 **{steal:,}** 코인을 가져왔습니다!\n🪙 현재: **{new_money:,}**",
            view=None
        )


# ============================================================
# 미스터리 코인 상자 경매
# ============================================================
# 기획 확률:
#  주작 1%  : 15,000,000 + 잭팟
#  초대박 14%: 5,000,000 ~ 15,000,000
#  대박 35%  : 2,000,000 ~ 4,000,000
#  본전 30%  : 500,000 ~ 1,500,000
#  꽝 20%    : 10 ~ 1,000

active_auctions = {}  # channel_id -> True
# 채널별 미스터리 잭팟 누적 (메모리, 서버 재시작 시 초기화)
mystery_jackpots = defaultdict(int)  # channel_id -> amount


def roll_mystery_box(channel_id: int):
    """상자 개봉 결과. (grade, reward, jackpot_taken)"""
    roll = random.random()
    jackpot = mystery_jackpots.get(channel_id, 0)
    if roll < 0.01:
        # 주작: 1500만 + 잭팟 전액
        reward = 15_000_000 + jackpot
        mystery_jackpots[channel_id] = 0
        return "🏆 주작 (Wtf)!!!", reward, jackpot
    if roll < 0.15:
        return "💎 초대박 JACKPOT!", random.randint(5_000_000, 15_000_000), 0
    if roll < 0.50:
        return "🎉 대박!", random.randint(2_000_000, 4_000_000), 0
    if roll < 0.80:
        return "✨ 본전치기급", random.randint(500_000, 1_500_000), 0
    return "💣 대박 꽝 (Trap)...", random.randint(10, 1_000), 0


class MysteryAuctionView(discord.ui.View):
    START_BID = 100_000
    STEP_SMALL = 50_000
    STEP_BIG = 200_000
    DURATION = 60

    def __init__(self, game_id, host_id: str, channel_id: int):
        super().__init__(timeout=180)
        self.game_id = game_id
        self.host_id = host_id
        self.channel_id = channel_id
        self.current_bid = self.START_BID
        self.highest_bidder = None
        self.highest_name = None
        self.ends_at = datetime.utcnow() + timedelta(seconds=self.DURATION)
        self.finished = False
        self.message = None
        self.lock = asyncio.Lock()
        self.bid_count = 0

    def build_embed(self):
        remaining = max(0, int((self.ends_at - datetime.utcnow()).total_seconds()))
        bidder = f"<@{self.highest_bidder}>" if self.highest_bidder else "없음"
        jp = mystery_jackpots.get(self.channel_id, 0)
        embed = discord.Embed(
            title="🔨 미스터리 코인 상자 실시간 경매!",
            description=(
                "🎁 상자 내용물: 🪙 **???** 코인\n"
                "_(주작 / 초대박 / 대박 / 본전 / 꽝 중 하나)_\n\n"
                f"🏁 시작가: **{self.START_BID:,}**\n"
                f"📈 현재 최고가: **{self.current_bid:,}** 코인\n"
                f"👤 최고 입찰자: {bidder}\n"
                f"🔢 입찰 횟수: **{self.bid_count}**회\n"
                f"⏱️ 남은 시간: **{remaining}초**\n"
                f"💰 누적 잭팟: **{jp:,}** 코인 _(주작 당첨 시 합산)_"
            ),
            color=discord.Color.gold()
        )
        embed.add_field(
            name="확률 안내",
            value=(
                "🏆 주작 1% · 💎 초대박 14% · 🎉 대박 35%\n"
                "✨ 본전 30% · 💣 꽝 20%"
            ),
            inline=False
        )
        embed.set_footer(text="입찰 시 5초 미만이면 5초로 연장 · 낙찰자만 최종 금액 차감")
        return embed

    async def run_timer(self):
        try:
            while not self.finished:
                await asyncio.sleep(1)
                remaining = (self.ends_at - datetime.utcnow()).total_seconds()
                if remaining <= 0:
                    await self.finalize()
                    break
                # 마지막 15초는 자주 갱신
                if self.message and remaining <= 15:
                    try:
                        await self.message.edit(embed=self.build_embed(), view=self)
                    except Exception:
                        pass
                elif self.message and int(remaining) % 5 == 0:
                    try:
                        await self.message.edit(embed=self.build_embed(), view=self)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass
        finally:
            active_auctions.pop(self.channel_id, None)

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
            if (player["money"] or 0) < amount:
                await interaction.response.send_message(
                    f"🔴 돈이 부족합니다.\n필요: **{amount:,}** / 보유: **{player['money']:,}**",
                    ephemeral=True
                )
                return

            self.current_bid = amount
            self.highest_bidder = str(interaction.user.id)
            self.highest_name = interaction.user.display_name
            self.bid_count += 1

            remaining = (self.ends_at - datetime.utcnow()).total_seconds()
            if remaining < 5:
                self.ends_at = datetime.utcnow() + timedelta(seconds=5)

            await interaction.response.send_message(
                f"💥 <@{interaction.user.id}> 님이 **{amount:,}** 코인으로 응수!\n"
                f"⏱️ 남은 시간: **{max(0, int((self.ends_at - datetime.utcnow()).total_seconds()))}초**",
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

        channel = self.message.channel if self.message else None

        if not self.highest_bidder:
            embed = discord.Embed(
                title="🔨 경매 유찰",
                description="입찰자가 없어 경매가 취소되었습니다.",
                color=discord.Color.dark_grey()
            )
            if self.message:
                try:
                    await self.message.edit(embed=embed, view=None)
                except Exception:
                    pass
            return

        # 낙찰 차감
        connection = await get_db()
        try:
            ok = await connection.fetchval(
                """
                UPDATE players SET money = money - $1, last_money_loss = $1, updated_at = NOW()
                WHERE user_id = $2 AND money >= $1 RETURNING money
                """,
                self.current_bid, self.highest_bidder
            )
            if ok is None:
                if self.message:
                    try:
                        await self.message.edit(
                            content="낙찰자 잔액 부족으로 경매가 취소되었습니다.",
                            embed=None, view=None
                        )
                    except Exception:
                        pass
                return
            # 낙찰금 일부(10%)를 잭팟에 적립
            pool_add = self.current_bid // 10
            mystery_jackpots[self.channel_id] = mystery_jackpots.get(self.channel_id, 0) + pool_add
        finally:
            await connection.close()

        # 개봉 연출
        if channel:
            try:
                await channel.send(
                    f"🔨 **탕! 탕! 탕!** 낙찰!\n"
                    f"🎉 최종 낙찰자: <@{self.highest_bidder}> (**{self.current_bid:,}** 코인)\n"
                    f"📦 상자를 열어보는 중..."
                )
                await asyncio.sleep(2)
            except Exception:
                pass

        grade, reward, jp_taken = roll_mystery_box(self.channel_id)

        connection = await get_db()
        try:
            new_money = await connection.fetchval(
                "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2 RETURNING money",
                reward, self.highest_bidder
            )
        finally:
            await connection.close()

        jp_line = f"\n💰 잭팟 **{jp_taken:,}** 포함!" if jp_taken > 0 else ""
        net = reward - self.current_bid
        net_txt = f"+{net:,}" if net >= 0 else f"{net:,}"

        embed = discord.Embed(
            title="🎁 미스터리 상자 개봉!",
            description=(
                f"🎉 낙찰자: <@{self.highest_bidder}>\n"
                f"💸 낙찰가: **{self.current_bid:,}** 코인\n\n"
                f"💥 결과: **{grade}**\n"
                f"🪙 획득: **{reward:,}** 코인{jp_line}\n"
                f"📊 손익: **{net_txt}**\n"
                f"🪙 현재 보유: **{new_money:,}**"
            ),
            color=discord.Color.gold() if net >= 0 else discord.Color.dark_grey()
        )
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    @discord.ui.button(label="+50,000 입찰", emoji="✋", style=discord.ButtonStyle.primary, row=0)
    async def bid_50k(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.place_bid(interaction, self.current_bid + self.STEP_SMALL)

    @discord.ui.button(label="+200,000 찌르기", emoji="🚀", style=discord.ButtonStyle.danger, row=0)
    async def bid_200k(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.place_bid(interaction, self.current_bid + self.STEP_BIG)

    @discord.ui.button(label="현재가 확인", emoji="📊", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.build_embed(), ephemeral=True)


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
    def __init__(self, game_id, user_id, guild_id, bet: int, channel_id: int = None):
        super().__init__(timeout=90)  # 90초 무응답 = 중퇴 패배
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.bet = bet
        self.deck = create_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.doubled = False
        self.public_message = None  # 채널 공개 메시지

    def build_embed(self, reveal=False):
        p_val = hand_value(self.player_hand)
        embed = discord.Embed(
            title="🃏 블랙잭 (공개 진행 · 중퇴 불가)",
            description=f"플레이어: <@{self.user_id}>\n⚠️ 제한시간 내 미응답 시 **배팅액 몰수**",
            color=discord.Color.dark_green()
        )
        if reveal:
            d_val = hand_value(self.dealer_hand)
            dealer_text = f"**{format_hand(self.dealer_hand)}**  (`합: {d_val}`)"
            dealer_name = f"딜러 (합 {d_val})"
        else:
            shown = self.dealer_hand[0]
            dealer_text = f"**{shown}**  + 🂠"
            dealer_name = f"딜러 (공개 {card_value(shown)})"
        embed.add_field(name=dealer_name, value=dealer_text, inline=False)
        embed.add_field(
            name=f"플레이어 (합 {p_val})",
            value=f"**{format_hand(self.player_hand)}**",
            inline=False
        )
        embed.add_field(name="배팅", value=f"**{self.bet:,}** 코인", inline=True)
        embed.set_footer(text="채널에 공개 진행됩니다. 메시지를 지워도 세션은 유지됩니다.")
        return embed

    async def on_timeout(self):
        """무응답 중퇴 → 배팅 몰수 + 도박/알바 일시 금지"""
        if self.finished:
            return
        self.finished = True
        clear_gamble(self.user_id)
        pen_sec = await get_timeout_penalty_seconds(self.game_id)
        apply_timeout_penalty(self.user_id, pen_sec)
        connection = await get_db()
        try:
            await connection.execute(
                "UPDATE players SET last_money_loss = $1, updated_at = NOW() WHERE user_id = $2",
                self.bet, str(self.user_id)
            )
            new_money = await connection.fetchval(
                "SELECT money FROM players WHERE user_id = $1", str(self.user_id)
            )
        finally:
            await connection.close()
        embed = self.build_embed(reveal=True)
        embed.color = discord.Color.dark_red()
        embed.add_field(
            name="결과",
            value=(
                f"⏰ **시간 초과 중퇴!** 배팅 **{self.bet:,}** 코인 몰수\n"
                f"🚫 패널티: **{pen_sec // 60}분** 동안 도박·알바 이용 불가"
            ),
            inline=False
        )
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)
        for child in self.children:
            child.disabled = True
        if self.public_message:
            try:
                await self.public_message.edit(embed=embed, view=None)
            except Exception:
                pass

    async def end_game(self, interaction, result: str, payout_mult: float):
        if self.finished:
            return
        self.finished = True
        self.stop()
        clear_gamble(self.user_id)
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
            elif payout_mult == 1.0:
                new_money = await connection.fetchval(
                    "UPDATE players SET money = money + $1, updated_at = NOW() WHERE user_id = $2 RETURNING money",
                    self.bet, str(self.user_id)
                )
            else:
                await connection.execute(
                    "UPDATE players SET last_money_loss = $1, updated_at = NOW() WHERE user_id = $2",
                    self.bet, str(self.user_id)
                )
                new_money = await connection.fetchval(
                    "SELECT money FROM players WHERE user_id = $1", str(self.user_id)
                )
        finally:
            await connection.close()

        embed = self.build_embed(reveal=True)
        embed.add_field(name="결과", value=result, inline=False)
        if payout > 0 and payout_mult > 1.0:
            embed.add_field(name="획득", value=f"+**{payout:,}** 코인", inline=True)
        elif payout_mult == 1.0 or "무승부" in result:
            embed.add_field(name="반환", value=f"**{self.bet:,}** 코인 반환", inline=True)
        else:
            embed.add_field(name="손실", value=f"-**{self.bet:,}** 코인", inline=True)
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)

        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception:
            if self.public_message:
                try:
                    await self.public_message.edit(embed=embed, view=None)
                except Exception:
                    pass

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
        # 추가 배팅 가능 여부 (초기 배팅은 이미 차감된 상태 → 남은 돈 >= 현재 bet 필요)
        player = await get_player(str(self.user_id), str(self.guild_id))
        have = player["money"] if player else 0
        if not player or have < self.bet:
            await interaction.response.send_message(
                f"🔴 **돈이 부족합니다.**\n"
                f"DOUBLE에 필요한 추가 금액: **{self.bet:,}**\n"
                f"현재 보유: **{have:,}**",
                ephemeral=True
            )
            return
        connection = await get_db()
        try:
            ok = await connection.fetchval(
                """
                UPDATE players SET money = money - $1, updated_at = NOW()
                WHERE user_id = $2 AND money >= $1
                RETURNING money
                """,
                self.bet, str(self.user_id)
            )
            if ok is None:
                await interaction.response.send_message(
                    f"🔴 **돈이 부족합니다.**\nDOUBLE에 필요한 추가 금액: **{self.bet:,}**",
                    ephemeral=True
                )
                return
        finally:
            await connection.close()
        self.bet *= 2
        self.doubled = True
        self.player_hand.append(self.deck.pop())
        val = hand_value(self.player_hand)
        if val > 21:
            await self.end_game(interaction, "💥 버스트! 패배 (DOUBLE)", 0)
        else:
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
        clear_gamble(self.user_id)
        embed = self.build_embed(reveal=True)
        embed.add_field(name="결과", value="🏳️ SURRENDER (절반 반환)", inline=False)
        embed.add_field(name="반환", value=f"**{half:,}** 코인", inline=True)
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)
        await interaction.response.edit_message(embed=embed, view=None)


class UnderdrawBlackjackView(discord.ui.View):
    """밑장빼기권: 블랙잭 시작 패 1장 랜덤 교체"""
    def __init__(self, bj_view: "BlackjackView"):
        super().__init__(timeout=30)
        self.bj = bj_view

    @discord.ui.button(label="밑장빼기 사용 (1장 랜덤 교체)", emoji="🎭", style=discord.ButtonStyle.primary)
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.bj.user_id:
            await interaction.response.defer()
            return
        ok = await remove_item_from_inventory(
            str(self.bj.user_id), str(self.bj.guild_id), "밑장빼기권", 1
        )
        if not ok:
            await interaction.response.send_message("밑장빼기권이 없습니다.", ephemeral=True)
            return
        idx = random.randint(0, len(self.bj.player_hand) - 1)
        old = self.bj.player_hand[idx]
        new = self.bj.deck.pop()
        self.bj.player_hand[idx] = new
        if hand_value(self.bj.player_hand) == 21 and len(self.bj.player_hand) == 2:
            payout = int(self.bj.bet * 2.5)
            connection = await get_db()
            try:
                new_money = await connection.fetchval(
                    "UPDATE players SET money = money + $1 WHERE user_id = $2 RETURNING money",
                    payout, str(self.bj.user_id)
                )
            finally:
                await connection.close()
            embed = self.bj.build_embed(reveal=True)
            embed.add_field(name="밑장빼기", value=f"{old} → {new}", inline=False)
            clear_gamble(self.bj.user_id)
            embed.add_field(name="결과", value="🎉 **블랙잭!** (×2.5)", inline=False)
            embed.add_field(name="획득", value=f"+**{payout:,}**", inline=True)
            embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)
            await interaction.response.edit_message(content=None, embed=embed, view=None)
            return
        await interaction.response.edit_message(
            content=f"🎭 밑장빼기! **{old}** → **{new}**",
            embed=self.bj.build_embed(),
            view=self.bj
        )

    @discord.ui.button(label="사용 안 함", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.bj.user_id:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(content=None, embed=self.bj.build_embed(), view=self.bj)


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

        if is_user_gambling(interaction.user.id):
            sess = active_gambles[int(interaction.user.id)]
            await interaction.response.send_message(
                f"🔒 이미 **{sess['type']}** 진행 중입니다.\n"
                f"배팅 **{sess['bet']:,}** — 종료 전까지 다른 도박을 시작할 수 없습니다.",
                ephemeral=True
            )
            return
        pen = get_penalty_remaining(interaction.user.id)
        if pen > 0:
            await interaction.response.send_message(
                f"🚫 시간 초과 패널티 중입니다.\n"
                f"도박·알바 이용 불가 — 남은 시간: **{format_seconds(pen)}**",
                ephemeral=True
            )
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
                await interaction.response.send_message("배팅 실패 (잔액 부족)", ephemeral=True)
                return
        finally:
            await connection.close()

        view = BlackjackView(
            self.game_id, interaction.user.id, interaction.guild.id, bet,
            channel_id=interaction.channel.id
        )
        # 자연 블랙잭
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
            await interaction.response.send_message(
                content=f"🃏 <@{interaction.user.id}> 블랙잭 결과",
                embed=embed
            )
            return

        # 세션 등록 (중퇴 방지)
        register_gamble(interaction.user.id, "블랙잭", bet, interaction.channel.id, self.game_id)

        inv = await get_player_inventory(str(interaction.user.id), str(interaction.guild.id))
        if inv.get("밑장빼기권", 0) > 0:
            under_view = UnderdrawBlackjackView(view)
            msg = await interaction.channel.send(
                content=f"🃏 <@{interaction.user.id}> 블랙잭 시작! (공개 진행 · 중퇴 시 배팅 몰수)",
                embed=view.build_embed(),
                view=under_view
            )
            view.public_message = msg
            await interaction.response.send_message(
                "블랙잭이 채널에 **공개**로 시작되었습니다. (지워도 세션 유지)",
                ephemeral=True
            )
            return

        msg = await interaction.channel.send(
            content=f"🃏 <@{interaction.user.id}> 블랙잭 시작! (공개 진행 · 중퇴 시 배팅 몰수)",
            embed=view.build_embed(),
            view=view
        )
        view.public_message = msg
        await interaction.response.send_message(
            "블랙잭이 채널에 **공개**로 시작되었습니다. 제한시간 내 미응답 시 배팅 몰수.",
            ephemeral=True
        )


# ============================================================
# 에이스 브레이커 (싱글 vs 봇 — 기획안 반영)
# ============================================================
# 규칙 요약:
# - 카드 풀: 2~9, ACE, JOKER (여러 덱 취급)
# - JOKER는 숫자/ACE 자리가 아님. 뽑히면 별도 보관 + 그 자리 일반카드 재뽑기
# - JOKER 최대 1장. 두 번째 JOKER는 버리고 다시 뽑기
# - 카드는 1장씩 버튼으로 드로우
# - 3장 다 뽑은 뒤 멀리건(전체 재뽑기) 1회 선택 가능
# - 그 다음 배팅 → 비교 (높음→낮음→높음)
# - ACE 1장당 배율 +0.7 (최대 3.1배), 에이스브레이커 성공 시 최소 2.1배
# - JOKER 보유 여부는 상대에게 비공개

def _ab_make_pool():
    """에이스 브레이커용 카드 풀 (충분히 크게)"""
    pool = []
    for _ in range(4):  # 여러 덱 느낌
        pool.extend([str(i) for i in range(2, 10)])
        pool.extend(["A", "A", "J", "J"])
    random.shuffle(pool)
    return pool


def _ab_card_str(c):
    if c == "A":
        return "🅰️ ACE"
    if c == "J":
        return "🃏 JOKER"
    return f"**{c}**"


def _ab_draw_one_normal(pool, has_joker: bool):
    """
    일반 카드 1장 드로우.
    JOKER가 나오면: has_joker가 False면 JOKER 획득 + 일반카드 재뽑기
                    True면 버리고 일반카드 재뽑기
    반환: (card, got_joker: bool)  got_joker=True면 이번에 JOKER를 새로 얻음
    """
    got_joker = False
    while True:
        if not pool:
            pool.extend(_ab_make_pool())
        c = pool.pop()
        if c == "J":
            if not has_joker and not got_joker:
                got_joker = True
                has_joker = True
                # 자리용 일반 카드 계속 뽑기
                continue
            else:
                # 이미 JOKER 있음 → 버리고 다시
                continue
        return c, got_joker


class AceBreakerView(discord.ui.View):
    """
    싱글 에이스 브레이커 플로우:
    draw(1) → draw(2) → draw(3) → mulligan? → bet → resolve
    """
    PHASE_DRAW = "draw"
    PHASE_MULLIGAN = "mulligan"
    PHASE_BET = "bet"
    PHASE_DONE = "done"

    def __init__(self, game_id, user_id, guild_id, channel_id: int = None):
        super().__init__(timeout=120)
        self.game_id = game_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.phase = self.PHASE_DRAW
        self.draw_index = 0
        self.player_cards = [None, None, None]
        self.player_joker = False
        self.bot_cards = [None, None, None]
        self.bot_joker = False
        self.pool = _ab_make_pool()
        self.mulligan_used = False
        self.bet = 0
        self.finished = False
        self.public_message = None
        self._rebuild_buttons()

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        bet = self.bet
        clear_gamble(self.user_id)
        pen_sec = await get_timeout_penalty_seconds(self.game_id)
        apply_timeout_penalty(self.user_id, pen_sec)
        msg = "⏰ **시간 초과 중퇴!**"
        if bet > 0:
            connection = await get_db()
            try:
                await connection.execute(
                    "UPDATE players SET last_money_loss = $1, updated_at = NOW() WHERE user_id = $2",
                    bet, str(self.user_id)
                )
            finally:
                await connection.close()
            msg += f" 배팅 **{bet:,}** 코인 몰수"
        else:
            msg += " (배팅 전 종료)"
        msg += f"\n🚫 패널티: **{pen_sec // 60}분** 동안 도박·알바 이용 불가"
        embed = self.build_embed(reveal_bot=True)
        embed.color = discord.Color.dark_red()
        embed.add_field(name="결과", value=msg, inline=False)
        if self.public_message:
            try:
                await self.public_message.edit(content=f"<@{self.user_id}> {msg}", embed=embed, view=None)
            except Exception:
                pass

    def _rebuild_buttons(self):
        self.clear_items()
        if self.phase == self.PHASE_DRAW:
            n = self.draw_index + 1
            btn = discord.ui.Button(
                label=f"{n}번째 카드 뽑기",
                emoji="🃏",
                style=discord.ButtonStyle.primary,
                custom_id="ab_draw"
            )
            btn.callback = self.on_draw
            self.add_item(btn)
        elif self.phase == self.PHASE_MULLIGAN:
            yes = discord.ui.Button(label="멀리건 (전체 다시 뽑기)", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="ab_mull_yes")
            no = discord.ui.Button(label="이 패로 진행", emoji="✅", style=discord.ButtonStyle.success, custom_id="ab_mull_no")
            yes.callback = self.on_mulligan_yes
            no.callback = self.on_mulligan_no
            self.add_item(yes)
            self.add_item(no)
        elif self.phase == self.PHASE_BET:
            bet_btn = discord.ui.Button(label="배팅하기", emoji="💰", style=discord.ButtonStyle.success, custom_id="ab_bet")
            bet_btn.callback = self.on_bet_click
            self.add_item(bet_btn)

    def _hand_display(self, cards, joker, hide_joker=False):
        parts = []
        for c in cards:
            if c is None:
                parts.append("🂠")
            else:
                parts.append(_ab_card_str(c))
        jtxt = ""
        if joker and not hide_joker:
            jtxt = " + 🃏 JOKER"
        elif joker and hide_joker:
            jtxt = ""  # 상대에게 비공개
        return " | ".join(parts) + jtxt

    def build_embed(self, reveal_bot=False):
        embed = discord.Embed(
            title="🃏 에이스 브레이커 (공개 진행 · 중퇴 불가)",
            description=f"플레이어: <@{self.user_id}>\n⚠️ 제한시간 내 미응답 시 중퇴 처리",
            color=discord.Color.purple()
        )
        embed.add_field(
            name="당신의 패",
            value=self._hand_display(self.player_cards, self.player_joker, hide_joker=False),
            inline=False
        )
        if reveal_bot:
            embed.add_field(
                name="상대 패",
                value=self._hand_display(self.bot_cards, self.bot_joker, hide_joker=False),
                inline=False
            )
        else:
            # 상대 카드/조커 비공개
            hidden = " | ".join("🂠" if c is None else "🂠" for c in self.bot_cards) if any(c is None for c in self.bot_cards) else "🂠 | 🂠 | 🂠"
            embed.add_field(name="상대 패", value=hidden, inline=False)

        if self.phase == self.PHASE_DRAW:
            embed.add_field(name="단계", value=f"카드 드로우 ({self.draw_index}/3)", inline=True)
        elif self.phase == self.PHASE_MULLIGAN:
            embed.add_field(name="단계", value="멀리건 선택 (이 판 1회만)", inline=True)
        elif self.phase == self.PHASE_BET:
            embed.add_field(name="단계", value="배팅", inline=True)
        if self.bet > 0:
            embed.add_field(name="배팅액", value=f"**{self.bet:,}** 코인", inline=True)
        embed.set_footer(
            text="비교: 높음 → 낮음 → 높음 | ACE 무조건 승 (JOKER에 막힘) | JOKER는 상대에게 비공개"
        )
        return embed

    async def on_draw(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        if self.phase != self.PHASE_DRAW:
            await interaction.response.defer()
            return

        card, got_joker = _ab_draw_one_normal(self.pool, self.player_joker)
        if got_joker:
            self.player_joker = True
        self.player_cards[self.draw_index] = card
        self.draw_index += 1

        msg_extra = ""
        if got_joker:
            msg_extra = "\n🃏 **JOKER 획득!** (자리에는 일반 카드가 들어갑니다. JOKER는 별도 보관)"

        if self.draw_index >= 3:
            # 봇도 3장 드로우 (즉시)
            self._draw_bot_hand()
            if not self.mulligan_used:
                self.phase = self.PHASE_MULLIGAN
            else:
                self.phase = self.PHASE_BET
            self._rebuild_buttons()
            await interaction.response.edit_message(
                content=f"3장 드로우 완료!{msg_extra}",
                embed=self.build_embed(),
                view=self
            )
        else:
            self._rebuild_buttons()
            await interaction.response.edit_message(
                content=f"{self.draw_index}번째 카드: {_ab_card_str(card)}{msg_extra}",
                embed=self.build_embed(),
                view=self
            )

    def _draw_bot_hand(self):
        self.bot_cards = [None, None, None]
        self.bot_joker = False
        for i in range(3):
            card, got = _ab_draw_one_normal(self.pool, self.bot_joker)
            if got:
                self.bot_joker = True
            self.bot_cards[i] = card

    async def on_mulligan_yes(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        if self.phase != self.PHASE_MULLIGAN or self.mulligan_used:
            await interaction.response.send_message("멀리건을 사용할 수 없습니다.", ephemeral=True)
            return
        self.mulligan_used = True
        self.player_cards = [None, None, None]
        self.player_joker = False
        self.draw_index = 0
        self.phase = self.PHASE_DRAW
        self._rebuild_buttons()
        await interaction.response.edit_message(
            content="🔄 **멀리건!** 1번째 카드부터 다시 뽑으세요. (이번 판 추가 멀리건 불가)",
            embed=self.build_embed(),
            view=self
        )

    async def on_mulligan_no(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        self.phase = self.PHASE_BET
        self._rebuild_buttons()
        await interaction.response.edit_message(
            content="패 확정! 배팅을 진행하세요.",
            embed=self.build_embed(),
            view=self
        )

    async def on_bet_click(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id or self.finished:
            await interaction.response.defer()
            return
        modal = AceBreakerBetAmountModal(self)
        await interaction.response.send_modal(modal)

    async def resolve_after_bet(self, interaction: discord.Interaction, bet: int):
        """배팅 확정 후 비교 및 정산"""
        self.bet = bet
        self.finished = True
        self.phase = self.PHASE_DONE
        self.stop()
        clear_gamble(self.user_id)

        # JOKER 자동 사용: 상대 ACE가 있는 라운드에 우선 사용
        # 라운드 결과: "win" | "lose" | "draw"
        p_joker_left = self.player_joker
        b_joker_left = self.bot_joker
        results = []  # "win"/"lose"/"draw"
        wins = 0
        losses = 0
        draws = 0
        ace_break_success = False
        round_names = ["높음", "낮음", "높음"]
        want_high_list = [True, False, True]

        for i in range(3):
            p = self.player_cards[i]
            b = self.bot_cards[i]
            want_high = want_high_list[i]
            p_is_ace = (p == "A")
            b_is_ace = (b == "A")

            if b_is_ace and p_joker_left:
                p_joker_left = False
                ace_break_success = True
                outcome = "win"
            elif p_is_ace and b_joker_left:
                b_joker_left = False
                outcome = "lose"
            elif p_is_ace and b_is_ace:
                # ACE vs ACE → 무승부
                outcome = "draw"
            elif p_is_ace:
                outcome = "win"
            elif b_is_ace:
                outcome = "lose"
            else:
                # 숫자 비교 — 같으면 무승부
                if int(p) == int(b):
                    outcome = "draw"
                elif want_high:
                    outcome = "win" if int(p) > int(b) else "lose"
                else:
                    outcome = "win" if int(p) < int(b) else "lose"

            results.append(outcome)
            if outcome == "win":
                wins += 1
            elif outcome == "lose":
                losses += 1
            else:
                draws += 1

        ace_count = sum(1 for c in self.player_cards if c == "A")

        # 게임 결과: 승수 비교. 동점이면 배팅 환불
        is_tie_game = (wins == losses)
        if is_tie_game:
            mult = 1.0  # 환불
            payout = self.bet
            result = f"🤝 동점! ({wins}승 {losses}패 {draws}무) — 배팅액 반환"
        elif wins > losses:
            mult = 2.0 + ace_count * 0.7
            if ace_break_success:
                mult = max(mult, 2.1)
            mult = min(mult, 3.1)
            payout = int(self.bet * mult)
            result = f"🎉 승리! ({wins}승 {losses}패 {draws}무) ×**{mult:.1f}**"
        else:
            mult = 0
            payout = 0
            result = f"😢 패배 ({wins}승 {losses}패 {draws}무)"

        connection = await get_db()
        try:
            if payout > 0:
                new_money = await connection.fetchval(
                    """
                    UPDATE players SET money = money + $1, updated_at = NOW()
                    WHERE user_id = $2 RETURNING money
                    """,
                    payout, str(self.user_id)
                )
            else:
                await connection.execute(
                    """
                    UPDATE players SET last_money_loss = $1, updated_at = NOW()
                    WHERE user_id = $2
                    """,
                    self.bet, str(self.user_id)
                )
                new_money = await connection.fetchval(
                    "SELECT money FROM players WHERE user_id = $1", str(self.user_id)
                )
        finally:
            await connection.close()

        embed = self.build_embed(reveal_bot=True)
        detail = []
        for i, (outcome, name) in enumerate(zip(results, round_names)):
            mark = {"win": "✅", "lose": "❌", "draw": "➖"}.get(outcome, "?")
            detail.append(
                f"{i+1}라운드({name}): {mark}  "
                f"나 {_ab_card_str(self.player_cards[i])} vs 상대 {_ab_card_str(self.bot_cards[i])}"
            )
        embed.add_field(name="라운드 결과", value="\n".join(detail), inline=False)
        if ace_break_success:
            embed.add_field(name="에이스 브레이커", value="🃏 상대 ACE를 JOKER로 차단 성공!", inline=False)
        if self.player_joker:
            embed.add_field(name="내 JOKER", value="보유했음 (상대에게는 비공개였음)", inline=True)
        if self.bot_joker:
            embed.add_field(name="상대 JOKER", value="보유했음", inline=True)
        embed.add_field(name="최종", value=result, inline=False)
        if is_tie_game:
            embed.add_field(name="반환", value=f"**{self.bet:,}** 코인 환불", inline=True)
        elif payout > 0:
            embed.add_field(name="획득", value=f"+**{payout:,}**", inline=True)
        else:
            embed.add_field(name="손실", value=f"-**{self.bet:,}**", inline=True)
        embed.add_field(name="현재 코인", value=f"**{new_money:,}**", inline=True)

        try:
            await interaction.response.edit_message(content=None, embed=embed, view=None)
        except Exception:
            await interaction.followup.send(embed=embed, ephemeral=True)


class AceBreakerBetAmountModal(discord.ui.Modal, title="💰 에이스 브레이커 배팅"):
    def __init__(self, parent: AceBreakerView):
        super().__init__()
        self.parent = parent
        self.amount = discord.ui.TextInput(
            label="배팅 금액 (상대도 동일 금액 매칭)",
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

        player = await get_player(str(self.parent.user_id), str(self.parent.guild_id))
        if not player or player["money"] < bet:
            await interaction.response.send_message(
                f"🔴 돈이 부족합니다.\n보유: **{(player['money'] if player else 0):,}** / 필요: **{bet:,}**",
                ephemeral=True
            )
            return

        connection = await get_db()
        try:
            ok = await connection.fetchval(
                """
                UPDATE players SET money = money - $1, updated_at = NOW()
                WHERE user_id = $2 AND money >= $1 RETURNING money
                """,
                bet, str(self.parent.user_id)
            )
            if ok is None:
                await interaction.response.send_message("🔴 돈이 부족합니다.", ephemeral=True)
                return
        finally:
            await connection.close()

        # 세션 배팅액 갱신 (중퇴 시 몰수 금액)
        uid = int(self.parent.user_id)
        if uid in active_gambles:
            active_gambles[uid]["bet"] = bet
        else:
            register_gamble(
                uid, "에이스 브레이커", bet,
                self.parent.channel_id or 0, self.parent.game_id
            )

        await self.parent.resolve_after_bet(interaction, bet)


class UnderdrawAceBreakerView(discord.ui.View):
    """밑장빼기권: 에이스 브레이커 시작 전 덱 한 번 섞기 + 알림"""
    def __init__(self, ab_view: "AceBreakerView"):
        super().__init__(timeout=30)
        self.ab = ab_view

    @discord.ui.button(label="밑장빼기 사용", emoji="🎭", style=discord.ButtonStyle.primary)
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ab.user_id:
            await interaction.response.defer()
            return
        ok = await remove_item_from_inventory(
            str(self.ab.user_id), str(self.ab.guild_id), "밑장빼기권", 1
        )
        if not ok:
            await interaction.response.send_message("밑장빼기권이 없습니다.", ephemeral=True)
            return
        # 풀 재섞기 + 한 장 미리 버림 (랜덤 교체 느낌)
        random.shuffle(self.ab.pool)
        if self.ab.pool:
            self.ab.pool.pop()
        await interaction.response.edit_message(
            content="🎭 밑장빼기 사용! 덱이 흔들렸습니다.\n🃏 1번째 카드부터 뽑으세요!",
            embed=self.ab.build_embed(),
            view=self.ab
        )

    @discord.ui.button(label="사용 안 함", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ab.user_id:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            content="🃏 **에이스 브레이커**\n1번째 카드부터 순서대로 뽑으세요!",
            embed=self.ab.build_embed(),
            view=self.ab
        )


class AceBreakerStartView(discord.ui.View):
    """에이스 브레이커 시작 (배팅은 카드 뽑은 뒤)"""
    def __init__(self, game_id):
        super().__init__(timeout=60)
        self.game_id = game_id

    @discord.ui.button(label="에이스 브레이커 시작", emoji="🅰️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_gambling(interaction.user.id):
            await interaction.response.send_message("이미 도박 진행 중입니다.", ephemeral=True)
            return
        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 게임을 할 수 없습니다.", ephemeral=True)
            return
        if player["money"] < 20_000:
            await interaction.response.send_message(
                f"🔴 돈이 부족합니다. 최소 20,000 코인이 필요합니다.\n보유: **{player['money']:,}**",
                ephemeral=True
            )
            return
        view = AceBreakerView(
            self.game_id, interaction.user.id, interaction.guild.id,
            channel_id=interaction.channel.id
        )
        register_gamble(interaction.user.id, "에이스 브레이커", 0, interaction.channel.id, self.game_id)
        msg = await interaction.channel.send(
            content=f"🃏 <@{interaction.user.id}> 에이스 브레이커 시작! (공개 · 중퇴 불가)",
            embed=view.build_embed(),
            view=view
        )
        view.public_message = msg
        await interaction.response.send_message("공개 채널에서 시작되었습니다.", ephemeral=True)


# ============================================================
# 멀티 게임 (블랙잭 2~4인 / 에이스브레이커 1v1)
# ============================================================

active_multi_lobbies = {}  # channel_id -> lobby view


class MultiLobbyView(discord.ui.View):
    """채널 모집 → 15초 후 스레드 생성 → 스레드에서 준비"""

    def __init__(self, game_id, host_id, guild_id, channel_id, mode: str, min_bet: int):
        super().__init__(timeout=20)
        self.game_id = game_id
        self.host_id = int(host_id)
        self.guild_id = guild_id
        self.channel_id = int(channel_id)
        self.mode = mode  # "bj" | "ab"
        self.min_bet = min_bet
        self.max_players = 4 if mode == "bj" else 2
        self.min_players = 2
        self.players = []  # [{user_id, name, bet}]
        self.started = False
        self.cancelled = False
        self.message = None
        self.ends_at = datetime.utcnow() + timedelta(seconds=15)

    def player_list_text(self):
        if not self.players:
            return "없음"
        lines = []
        for i, p in enumerate(self.players, 1):
            lines.append(f"{i}. <@{p['user_id']}>")
        return "\n".join(lines)

    def build_embed(self):
        left = max(0, int((self.ends_at - datetime.utcnow()).total_seconds()))
        title = "🃏 멀티 블랙잭 모집" if self.mode == "bj" else "🅰️ 에이스 브레이커 1v1 모집"
        embed = discord.Embed(
            title=title,
            description=(
                f"호스트: <@{self.host_id}>\n"
                f"인원: **{len(self.players)}/{self.max_players}** (최소 {self.min_players})\n"
                f"⏱️ 모집 종료: **{left}초**\n"
                f"최소 배팅: **{self.min_bet:,}**\n\n"
                f"**참가자**\n{self.player_list_text()}"
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="15초 후 자동 진행 · 2명 미만이면 취소 · 인원 가득 차면 즉시 진행")
        return embed

    async def run_timer(self):
        try:
            while not self.started and not self.cancelled:
                await asyncio.sleep(1)
                left = (self.ends_at - datetime.utcnow()).total_seconds()
                if left <= 0:
                    await self.try_start()
                    break
                if self.message:
                    try:
                        await self.message.edit(embed=self.build_embed(), view=self)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def try_start(self):
        if self.started or self.cancelled:
            return
        if len(self.players) < self.min_players:
            self.cancelled = True
            self.stop()
            active_multi_lobbies.pop(self.channel_id, None)
            if self.message:
                try:
                    await self.message.edit(
                        embed=discord.Embed(
                            title="❌ 모집 취소",
                            description=f"인원 부족 ({len(self.players)}/{self.min_players})",
                            color=discord.Color.dark_grey()
                        ),
                        view=None
                    )
                except Exception:
                    pass
            return
        self.started = True
        self.stop()
        active_multi_lobbies.pop(self.channel_id, None)
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=None)
            except Exception:
                pass
        await self.open_thread_phase()

    async def open_thread_phase(self):
        channel = self.message.channel if self.message else None
        if not channel:
            return
        mentions = " ".join(f"<@{p['user_id']}>" for p in self.players)
        try:
            thread = await channel.create_thread(
                name=f"{'멀티BJ' if self.mode == 'bj' else 'AB1v1'}-{self.game_id}-{random.randint(100,999)}",
                type=discord.ChannelType.public_thread,
                auto_archive_duration=60
            )
        except Exception as e:
            await channel.send(f"스레드 생성 실패: {e}")
            return

        ready_view = MultiThreadReadyView(
            self.game_id, self.guild_id, self.mode, self.players, self.min_bet, thread
        )
        await thread.send(
            content=(
                f"{mentions}\n"
                f"🎮 **멀티 게임 스레드**\n"
                f"15초 안에 **준비완료**를 눌러주세요!\n"
                f"전원이 준비하면 즉시 시작됩니다.\n"
                f"_(코인·선행 등 개인 정보는 본인에게만 표시)_"
            ),
            embed=ready_view.build_embed(),
            view=ready_view
        )
        asyncio.create_task(ready_view.run_timer())

    @discord.ui.button(label="참가", emoji="✅", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started or self.cancelled:
            await interaction.response.send_message("이미 종료된 모집입니다.", ephemeral=True)
            return
        uid = interaction.user.id
        if is_user_gambling(uid) or is_timeout_penalized(uid):
            await interaction.response.send_message("도박 중이거나 패널티 상태입니다.", ephemeral=True)
            return
        if any(p["user_id"] == uid for p in self.players):
            await interaction.response.send_message("이미 참가 중입니다.", ephemeral=True)
            return
        if len(self.players) >= self.max_players:
            await interaction.response.send_message("인원이 가득 찼습니다.", ephemeral=True)
            return
        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 참가할 수 없습니다.", ephemeral=True)
            return
        if (player["money"] or 0) < self.min_bet:
            await interaction.response.send_message(
                f"코인 부족 (최소 {self.min_bet:,}). 보유: **{player['money']:,}**",
                ephemeral=True
            )
            return
        self.players.append({
            "user_id": uid,
            "name": interaction.user.display_name,
            "bet": self.min_bet,
        })
        await interaction.response.send_message(
            f"참가 완료! (보유 코인 **{player['money']:,}** — 본인만 확인)",
            ephemeral=True
        )
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except Exception:
                pass
        if len(self.players) >= self.max_players:
            await self.try_start()

    @discord.ui.button(label="나가기", emoji="🚪", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        before = len(self.players)
        self.players = [p for p in self.players if p["user_id"] != uid]
        if len(self.players) == before:
            await interaction.response.send_message("참가 중이 아닙니다.", ephemeral=True)
            return
        await interaction.response.send_message("모집에서 나갔습니다.", ephemeral=True)
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except Exception:
                pass


class MultiThreadReadyView(discord.ui.View):
    def __init__(self, game_id, guild_id, mode, players, min_bet, thread):
        super().__init__(timeout=20)
        self.game_id = game_id
        self.guild_id = guild_id
        self.mode = mode
        self.players = players  # list of dicts
        self.min_bet = min_bet
        self.thread = thread
        self.ready = set()
        self.started = False
        self.ends_at = datetime.utcnow() + timedelta(seconds=15)

    def build_embed(self):
        left = max(0, int((self.ends_at - datetime.utcnow()).total_seconds()))
        lines = []
        for p in self.players:
            mark = "✅" if p["user_id"] in self.ready else "⏳"
            lines.append(f"{mark} <@{p['user_id']}>")
        return discord.Embed(
            title="🎮 준비 확인",
            description=(
                f"⏱️ **{left}초** 남음\n"
                f"전원 준비 시 즉시 시작\n\n" + "\n".join(lines)
            ),
            color=discord.Color.green()
        )

    async def run_timer(self):
        try:
            while not self.started:
                await asyncio.sleep(1)
                if (self.ends_at - datetime.utcnow()).total_seconds() <= 0:
                    await self.begin_game()
                    break
        except asyncio.CancelledError:
            pass

    async def begin_game(self):
        if self.started:
            return
        # 준비 안 한 사람은 제외 — 단 최소 인원 미달 시 취소
        ready_players = [p for p in self.players if p["user_id"] in self.ready]
        if len(ready_players) < 2:
            # 시간 초과 시 전원 참가 가정 (입장한 것으로 간주)
            ready_players = list(self.players)
        if len(ready_players) < 2:
            self.started = True
            self.stop()
            await self.thread.send("❌ 준비 인원 부족으로 멀티 게임이 취소되었습니다.")
            return
        self.started = True
        self.stop()
        for p in ready_players:
            register_gamble(p["user_id"], f"멀티-{self.mode}", self.min_bet, self.thread.id, self.game_id)

        if self.mode == "bj":
            await start_multi_blackjack(self.thread, self.game_id, self.guild_id, ready_players, self.min_bet)
        else:
            await start_multi_ace_breaker(self.thread, self.game_id, self.guild_id, ready_players, self.min_bet)

    @discord.ui.button(label="준비완료", emoji="✅", style=discord.ButtonStyle.success)
    async def ready_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if not any(p["user_id"] == uid for p in self.players):
            await interaction.response.send_message("이 게임 참가자가 아닙니다.", ephemeral=True)
            return
        self.ready.add(uid)
        player = await get_player(str(uid), str(self.guild_id))
        money = player["money"] if player else 0
        await interaction.response.send_message(
            f"준비 완료! (보유 **{money:,}** 코인 — 본인만 확인)",
            ephemeral=True
        )
        try:
            await interaction.message.edit(embed=self.build_embed(), view=self)
        except Exception:
            pass
        if len(self.ready) >= len(self.players):
            await self.begin_game()


async def start_multi_blackjack(thread, game_id, guild_id, players, min_bet):
    """멀티 블랙잭: 공통 딜러, 플레이어 순차 행동, 배팅은 min_bet 고정(선차감)"""
    deck = create_deck(4)
    random.shuffle(deck)
    dealer = [deck.pop(), deck.pop()]
    seats = []
    connection = await get_db()
    try:
        for p in players:
            ok = await connection.fetchval(
                "UPDATE players SET money = money - $1 WHERE user_id = $2 AND money >= $1 RETURNING money",
                min_bet, str(p["user_id"])
            )
            if ok is None:
                await thread.send(f"<@{p['user_id']}> 코인 부족으로 제외됩니다.")
                clear_gamble(p["user_id"])
                continue
            seats.append({
                "user_id": p["user_id"],
                "name": p["name"],
                "bet": min_bet,
                "hand": [deck.pop(), deck.pop()],
                "done": False,
                "bust": False,
                "stand": False,
            })
    finally:
        await connection.close()

    if len(seats) < 2:
        await thread.send("유효 플레이어 부족으로 취소. 차감된 배팅은 반환됩니다.")
        for s in seats:
            connection = await get_db()
            try:
                await connection.execute(
                    "UPDATE players SET money = money + $1 WHERE user_id = $2",
                    s["bet"], str(s["user_id"])
                )
            finally:
                await connection.close()
            clear_gamble(s["user_id"])
        return

    view = MultiBlackjackTableView(game_id, guild_id, deck, dealer, seats, thread)
    msg = await thread.send(embed=view.build_embed(), view=view)
    view.message = msg


class MultiBlackjackTableView(discord.ui.View):
    def __init__(self, game_id, guild_id, deck, dealer, seats, thread):
        super().__init__(timeout=90)
        self.game_id = game_id
        self.guild_id = guild_id
        self.deck = deck
        self.dealer = dealer
        self.seats = seats
        self.thread = thread
        self.turn = 0  # seat index
        self.finished = False
        self.message = None
        self._sync_buttons()

    def current(self):
        while self.turn < len(self.seats) and self.seats[self.turn]["done"]:
            self.turn += 1
        if self.turn >= len(self.seats):
            return None
        return self.seats[self.turn]

    def _sync_buttons(self):
        self.clear_items()
        hit = discord.ui.Button(label="HIT", emoji="🃏", style=discord.ButtonStyle.primary)
        stand = discord.ui.Button(label="STAND", emoji="✋", style=discord.ButtonStyle.secondary)
        hit.callback = self.on_hit
        stand.callback = self.on_stand
        self.add_item(hit)
        self.add_item(stand)

    def build_embed(self, reveal=False):
        cur = self.current()
        embed = discord.Embed(
            title="🃏 멀티 블랙잭",
            description="공개 진행 · 본인 차례에만 버튼 사용",
            color=discord.Color.dark_green()
        )
        if reveal:
            embed.add_field(
                name=f"딜러 (합 {hand_value(self.dealer)})",
                value=f"**{format_hand(self.dealer)}**",
                inline=False
            )
        else:
            embed.add_field(
                name="딜러",
                value=f"**{self.dealer[0]}** + 🂠",
                inline=False
            )
        for i, s in enumerate(self.seats):
            status = ""
            if s["bust"]:
                status = " — 💥버스트"
            elif s["stand"] or s["done"]:
                status = " — ✋스탠드"
            elif cur and cur["user_id"] == s["user_id"]:
                status = " — ▶️ 차례"
            val = hand_value(s["hand"])
            embed.add_field(
                name=f"{s['name']}{status}",
                value=f"**{format_hand(s['hand'])}** (합 {val}) · 배팅 {s['bet']:,}",
                inline=False
            )
        if cur and not reveal:
            embed.set_footer(text=f"현재 차례: {cur['name']}")
        return embed

    async def on_hit(self, interaction: discord.Interaction):
        cur = self.current()
        if not cur or interaction.user.id != cur["user_id"]:
            await interaction.response.send_message("당신 차례가 아닙니다.", ephemeral=True)
            return
        cur["hand"].append(self.deck.pop())
        if hand_value(cur["hand"]) > 21:
            cur["bust"] = True
            cur["done"] = True
            self.turn += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if self.current() is None:
            await self.finish(interaction)

    async def on_stand(self, interaction: discord.Interaction):
        cur = self.current()
        if not cur or interaction.user.id != cur["user_id"]:
            await interaction.response.send_message("당신 차례가 아닙니다.", ephemeral=True)
            return
        cur["stand"] = True
        cur["done"] = True
        self.turn += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if self.current() is None:
            await self.finish(interaction)

    async def on_timeout(self):
        if self.finished:
            return
        # 현재 턴 플레이어 자동 스탠드/패, 패널티
        cur = self.current()
        while cur:
            cur["done"] = True
            cur["stand"] = True
            pen = await get_timeout_penalty_seconds(self.game_id)
            apply_timeout_penalty(cur["user_id"], pen)
            self.turn += 1
            cur = self.current()
        await self.finish(None)

    async def finish(self, interaction):
        if self.finished:
            return
        self.finished = True
        self.stop()
        # 딜러 플레이
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        d_val = hand_value(self.dealer)
        d_bust = d_val > 21
        results = []
        connection = await get_db()
        try:
            for s in self.seats:
                clear_gamble(s["user_id"])
                p_val = hand_value(s["hand"])
                if s["bust"]:
                    outcome = "패"
                    payout = 0
                elif d_bust or p_val > d_val:
                    outcome = "승"
                    payout = s["bet"] * 2
                elif p_val == d_val:
                    outcome = "무"
                    payout = s["bet"]
                else:
                    outcome = "패"
                    payout = 0
                if payout > 0:
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2",
                        payout, str(s["user_id"])
                    )
                else:
                    await connection.execute(
                        "UPDATE players SET last_money_loss = $1 WHERE user_id = $2",
                        s["bet"], str(s["user_id"])
                    )
                results.append(f"<@{s['user_id']}> {outcome} (합{p_val}) → {'+'+format(payout,',') if payout else '-'+format(s['bet'],',')}")
        finally:
            await connection.close()

        embed = self.build_embed(reveal=True)
        embed.add_field(name="정산", value="\n".join(results), inline=False)
        if interaction:
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except Exception:
                try:
                    await interaction.message.edit(embed=embed, view=None)
                except Exception:
                    await self.thread.send(embed=embed)
        elif self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except Exception:
                await self.thread.send(embed=embed)


async def start_multi_ace_breaker(thread, game_id, guild_id, players, min_bet):
    """1v1: 선 랜덤 → 선 3장+멀리건 → 후 3장+멀리건 → 선배팅 → 후배팅(이상)"""
    if len(players) < 2:
        await thread.send("인원 부족")
        return
    a, b = players[0], players[1]
    if random.random() < 0.5:
        first, second = a, b
    else:
        first, second = b, a

    view = MultiAceBreakerView(game_id, guild_id, first, second, min_bet, thread)
    msg = await thread.send(
        content=(
            f"🅰️ **에이스 브레이커 1v1**\n"
            f"선(먼저 뽑기·선배팅): <@{first['user_id']}>\n"
            f"후: <@{second['user_id']}>\n"
            f"상대 패는 비공개입니다."
        ),
        embed=view.build_embed(),
        view=view
    )
    view.message = msg


class MultiAceBreakerView(discord.ui.View):
    """
    phase: first_draw → first_mull → second_draw → second_mull → first_bet → second_bet → resolve
    """
    def __init__(self, game_id, guild_id, first, second, min_bet, thread):
        super().__init__(timeout=180)
        self.game_id = game_id
        self.guild_id = guild_id
        self.first = first
        self.second = second
        self.min_bet = min_bet
        self.thread = thread
        self.message = None
        self.phase = "first_draw"
        self.draw_i = 0
        self.pool = _ab_make_pool()
        self.cards = {
            first["user_id"]: [None, None, None],
            second["user_id"]: [None, None, None],
        }
        self.joker = {first["user_id"]: False, second["user_id"]: False}
        self.mull_used = {first["user_id"]: False, second["user_id"]: False}
        self.bets = {first["user_id"]: 0, second["user_id"]: 0}
        self.finished = False
        self._rebuild()

    def _active_uid(self):
        if self.phase.startswith("first"):
            return self.first["user_id"]
        if self.phase.startswith("second"):
            return self.second["user_id"]
        return None

    def _rebuild(self):
        self.clear_items()
        if self.phase in ("first_draw", "second_draw"):
            n = self.draw_i + 1
            btn = discord.ui.Button(label=f"{n}번째 카드 뽑기", emoji="🃏", style=discord.ButtonStyle.primary)
            btn.callback = self.on_draw
            self.add_item(btn)
        elif self.phase in ("first_mull", "second_mull"):
            y = discord.ui.Button(label="멀리건", emoji="🔄", style=discord.ButtonStyle.danger)
            n = discord.ui.Button(label="이 패로", emoji="✅", style=discord.ButtonStyle.success)
            y.callback = self.on_mull_yes
            n.callback = self.on_mull_no
            self.add_item(y)
            self.add_item(n)
        elif self.phase in ("first_bet", "second_bet"):
            btn = discord.ui.Button(label="배팅하기", emoji="💰", style=discord.ButtonStyle.success)
            btn.callback = self.on_bet_click
            self.add_item(btn)

    def build_embed(self):
        embed = discord.Embed(title="🅰️ 에이스 브레이커 1v1", color=discord.Color.purple())
        for uid, label in ((self.first["user_id"], "선"), (self.second["user_id"], "후")):
            cards = self.cards[uid]
            # 본인/공개 전에는 상대 패 숨김 — 공개 임베드에서는 진행 중인 사람만 표시
            if uid == self._active_uid() or self.phase == "resolve":
                shown = self._hand_str(cards, self.joker[uid], hide=False)
            else:
                shown = "🂠 | 🂠 | 🂠" if any(c is None for c in cards) else "🂠 | 🂠 | 🂠 (비공개)"
                # 자기 패는 뽑은 만큼만 본인에게 ephemeral로 알려줌
                if all(c is not None for c in cards) and self.phase not in ("first_bet", "second_bet", "resolve"):
                    shown = "🂠 | 🂠 | 🂠 (비공개)"
            bet = self.bets[uid]
            embed.add_field(
                name=f"{label} <@{uid}>" + (f" · 배팅 {bet:,}" if bet else ""),
                value=shown,
                inline=False
            )
        embed.add_field(name="단계", value=self.phase, inline=True)
        embed.set_footer(text="상대 패 비공개 · 선배팅 후 후자는 그 이상만 배팅")
        return embed

    def _hand_str(self, cards, has_joker, hide=False):
        if hide:
            return "🂠 | 🂠 | 🂠"
        parts = []
        for c in cards:
            if c is None:
                parts.append("?")
            else:
                parts.append(_ab_card_str(c))
        s = " | ".join(parts)
        if has_joker:
            s += " + 🃏"
        return s

    async def _only_active(self, interaction):
        uid = self._active_uid()
        if interaction.user.id != uid:
            await interaction.response.send_message("당신 차례가 아닙니다.", ephemeral=True)
            return False
        return True

    async def on_draw(self, interaction: discord.Interaction):
        if not await self._only_active(interaction):
            return
        uid = interaction.user.id
        card, got_joker = _ab_draw_one_normal(self.pool, self.joker[uid])
        if got_joker:
            self.joker[uid] = True
        self.cards[uid][self.draw_i] = card
        self.draw_i += 1
        private = self._hand_str(self.cards[uid], self.joker[uid])
        if self.draw_i >= 3:
            self.draw_i = 0
            if self.phase == "first_draw":
                self.phase = "first_mull"
            else:
                self.phase = "second_mull"
            self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(f"당신의 패: **{private}**", ephemeral=True)

    async def on_mull_yes(self, interaction: discord.Interaction):
        if not await self._only_active(interaction):
            return
        uid = interaction.user.id
        if self.mull_used[uid]:
            await interaction.response.send_message("이미 멀리건 사용", ephemeral=True)
            return
        self.mull_used[uid] = True
        self.cards[uid] = [None, None, None]
        self.joker[uid] = False
        self.pool = _ab_make_pool()
        self.draw_i = 0
        self.phase = "first_draw" if uid == self.first["user_id"] else "second_draw"
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_mull_no(self, interaction: discord.Interaction):
        if not await self._only_active(interaction):
            return
        uid = interaction.user.id
        if uid == self.first["user_id"]:
            self.phase = "second_draw"
            self.draw_i = 0
        else:
            self.phase = "first_bet"
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_bet_click(self, interaction: discord.Interaction):
        if not await self._only_active(interaction):
            return
        min_req = self.min_bet
        if self.phase == "second_bet":
            min_req = max(self.min_bet, self.bets[self.first["user_id"]])
        modal = MultiABBetModal(self, min_req)
        await interaction.response.send_modal(modal)

    async def apply_bet(self, interaction, amount: int):
        uid = interaction.user.id
        connection = await get_db()
        try:
            ok = await connection.fetchval(
                "UPDATE players SET money = money - $1 WHERE user_id = $2 AND money >= $1 RETURNING money",
                amount, str(uid)
            )
            if ok is None:
                await interaction.response.send_message("코인 부족", ephemeral=True)
                return
        finally:
            await connection.close()
        self.bets[uid] = amount
        if uid in active_gambles:
            active_gambles[uid]["bet"] = amount
        if self.phase == "first_bet":
            self.phase = "second_bet"
            self._rebuild()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            await interaction.followup.send(f"선배팅 **{amount:,}** 확정 (보유는 본인만)", ephemeral=True)
        else:
            await self.resolve(interaction)

    async def resolve(self, interaction):
        self.finished = True
        self.phase = "resolve"
        self.stop()
        f_uid, s_uid = self.first["user_id"], self.second["user_id"]
        # 비교: 3라운드 높낮높
        want = [True, False, True]
        fw = fl = 0
        detail = []
        f_j, s_j = self.joker[f_uid], self.joker[s_uid]
        for i in range(3):
            fc, sc = self.cards[f_uid][i], self.cards[s_uid][i]
            # ACE / JOKER
            if sc == "A" and f_j:
                f_j = False
                out = "first"
            elif fc == "A" and s_j:
                s_j = False
                out = "second"
            elif fc == "A" and sc == "A":
                out = "draw"
            elif fc == "A":
                out = "first"
            elif sc == "A":
                out = "second"
            elif fc == sc:
                out = "draw"
            elif want[i]:
                out = "first" if int(fc) > int(sc) else "second"
            else:
                out = "first" if int(fc) < int(sc) else "second"
            if out == "first":
                fw += 1
            elif out == "second":
                fl += 1
            detail.append(f"{i+1}R: {_ab_card_str(fc)} vs {_ab_card_str(sc)} → {out}")

        # 정산: 각자 배팅. 승자가 상대 배팅만큼 획득 (간단 포트)
        pot = self.bets[f_uid] + self.bets[s_uid]
        if fw > fl:
            winner, loser = f_uid, s_uid
        elif fl > fw:
            winner, loser = s_uid, f_uid
        else:
            winner = None

        connection = await get_db()
        try:
            if winner is None:
                # 환불
                for uid in (f_uid, s_uid):
                    await connection.execute(
                        "UPDATE players SET money = money + $1 WHERE user_id = $2",
                        self.bets[uid], str(uid)
                    )
                result = "🤝 동점 — 배팅 반환"
            else:
                await connection.execute(
                    "UPDATE players SET money = money + $1 WHERE user_id = $2",
                    pot, str(winner)
                )
                await connection.execute(
                    "UPDATE players SET last_money_loss = $1 WHERE user_id = $2",
                    self.bets[loser], str(loser)
                )
                result = f"🎉 승자: <@{winner}> (팟 **{pot:,}**)"
        finally:
            await connection.close()

        clear_gamble(f_uid)
        clear_gamble(s_uid)
        embed = discord.Embed(
            title="🅰️ 1v1 결과",
            description=result + "\n\n" + "\n".join(detail),
            color=discord.Color.gold()
        )
        embed.add_field(
            name=f"선 <@{f_uid}>",
            value=self._hand_str(self.cards[f_uid], self.joker[f_uid]),
            inline=False
        )
        embed.add_field(
            name=f"후 <@{s_uid}>",
            value=self._hand_str(self.cards[s_uid], self.joker[s_uid]),
            inline=False
        )
        try:
            await interaction.response.edit_message(content=None, embed=embed, view=None)
        except Exception:
            await self.thread.send(embed=embed)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        pen = await get_timeout_penalty_seconds(self.game_id)
        for uid in (self.first["user_id"], self.second["user_id"]):
            apply_timeout_penalty(uid, pen)
            clear_gamble(uid)
            if self.bets.get(uid, 0) == 0:
                continue
            # 배팅된 금액은 이미 차감됨 → 몰수
        if self.message:
            try:
                await self.message.edit(
                    content="⏰ 시간 초과로 멀티 AB 종료 (중퇴 패널티 적용)",
                    view=None
                )
            except Exception:
                pass


class MultiABBetModal(discord.ui.Modal, title="💰 멀티 AB 배팅"):
    def __init__(self, parent: MultiAceBreakerView, min_req: int):
        super().__init__()
        self.parent = parent
        self.min_req = min_req
        self.amount = discord.ui.TextInput(
            label=f"배팅액 (최소 {min_req:,})",
            placeholder=str(min_req),
            required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet = int(self.amount.value.replace(",", "").strip())
            if bet < self.min_req:
                await interaction.response.send_message(
                    f"최소 **{self.min_req:,}** 이상이어야 합니다.", ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message("숫자만 입력", ephemeral=True)
            return
        await self.parent.apply_bet(interaction, bet)


class MultiMenuView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=60)
        self.game_id = game_id

    @discord.ui.button(label="멀티 블랙잭 (2~4인)", emoji="🃏", style=discord.ButtonStyle.primary)
    async def multi_bj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_lobby(interaction, "bj", 10_000)

    @discord.ui.button(label="에이스 브레이커 1v1", emoji="🅰️", style=discord.ButtonStyle.primary)
    async def multi_ab(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_lobby(interaction, "ab", 20_000)

    async def _open_lobby(self, interaction, mode, min_bet):
        ch_id = interaction.channel.id
        if active_multi_lobbies.get(ch_id):
            await interaction.response.send_message("이미 모집 중인 멀티 게임이 있습니다.", ephemeral=True)
            return
        if is_user_gambling(interaction.user.id) or is_timeout_penalized(interaction.user.id):
            await interaction.response.send_message("도박 중이거나 패널티 상태입니다.", ephemeral=True)
            return
        lobby = MultiLobbyView(
            self.game_id, interaction.user.id, interaction.guild.id, ch_id, mode, min_bet
        )
        # 호스트 자동 참가
        lobby.players.append({
            "user_id": interaction.user.id,
            "name": interaction.user.display_name,
            "bet": min_bet,
        })
        active_multi_lobbies[ch_id] = lobby
        msg = await interaction.channel.send(embed=lobby.build_embed(), view=lobby)
        lobby.message = msg
        asyncio.create_task(lobby.run_timer())
        await interaction.response.send_message("멀티 모집을 시작했습니다. (채널 공개)", ephemeral=True)


# ============================================================
# 게임 선택 메뉴
# ============================================================

class GameSelectView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=120)
        self.game_id = game_id

    @discord.ui.button(label="블랙잭 (싱글)", emoji="🃏", style=discord.ButtonStyle.primary, row=0)
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_gambling(interaction.user.id):
            sess = active_gambles[int(interaction.user.id)]
            await interaction.response.send_message(
                f"🔒 이미 **{sess['type']}** 진행 중입니다. 종료 후 다시 시도하세요.",
                ephemeral=True
            )
            return
        pen = get_penalty_remaining(interaction.user.id)
        if pen > 0:
            await interaction.response.send_message(
                f"🚫 시간 초과 패널티 중입니다.\n"
                f"도박·알바 이용 불가 — 남은 시간: **{format_seconds(pen)}**",
                ephemeral=True
            )
            return
        modal = BlackjackBetModal(self.game_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="멀티 게임", emoji="👥", style=discord.ButtonStyle.success, row=1)
    async def multi(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_gambling(interaction.user.id) or is_timeout_penalized(interaction.user.id):
            await interaction.response.send_message(
                "도박 중이거나 패널티 상태에서는 멀티 모집을 열 수 없습니다.",
                ephemeral=True
            )
            return
        embed = discord.Embed(
            title="👥 멀티 게임",
            description=(
                "**멀티 블랙잭** — 2~4인, 15초 모집 후 스레드에서 진행\n"
                "**에이스 브레이커 1v1** — 선/후 랜덤, 상대 패 비공개, 선배팅\n\n"
                "모집 완료 → 스레드 생성 → 15초 준비 → 전원 준비 시 즉시 시작"
            ),
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=MultiMenuView(self.game_id), ephemeral=True)

    @discord.ui.button(label="에이스 브레이커", emoji="🅰️", style=discord.ButtonStyle.primary, row=0)
    async def ace_breaker(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_gambling(interaction.user.id):
            sess = active_gambles[int(interaction.user.id)]
            await interaction.response.send_message(
                f"🔒 이미 **{sess['type']}** 진행 중입니다. 종료 후 다시 시도하세요.",
                ephemeral=True
            )
            return
        pen = get_penalty_remaining(interaction.user.id)
        if pen > 0:
            await interaction.response.send_message(
                f"🚫 시간 초과 패널티 중입니다.\n"
                f"도박·알바 이용 불가 — 남은 시간: **{format_seconds(pen)}**",
                ephemeral=True
            )
            return
        player = await get_or_create_player(interaction.user, interaction.guild)
        if not player["alive"] or player["eliminated"]:
            await interaction.response.send_message("탈락자는 게임을 할 수 없습니다.", ephemeral=True)
            return
        if player["money"] < 20_000:
            await interaction.response.send_message(
                f"🔴 돈이 부족합니다. 최소 20,000 코인이 필요합니다.\n보유: **{player['money']:,}**",
                ephemeral=True
            )
            return
        view = AceBreakerView(
            self.game_id, interaction.user.id, interaction.guild.id,
            channel_id=interaction.channel.id
        )
        register_gamble(interaction.user.id, "에이스 브레이커", 0, interaction.channel.id, self.game_id)
        inv = await get_player_inventory(str(interaction.user.id), str(interaction.guild.id))
        start_view = UnderdrawAceBreakerView(view) if inv.get("밑장빼기권", 0) > 0 else view
        msg = await interaction.channel.send(
            content=(
                f"🃏 <@{interaction.user.id}> **에이스 브레이커** 시작!\n"
                f"(공개 진행 · 중퇴/시간초과 시 배팅 몰수)"
            ),
            embed=view.build_embed(),
            view=start_view
        )
        view.public_message = msg
        await interaction.response.send_message(
            "에이스 브레이커가 채널에 **공개**로 시작되었습니다.",
            ephemeral=True
        )

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
        cd = await get_job_cooldown_for_game(self.game_id)
        charges, remaining = get_job_charges(interaction.user.id, cd)
        charge_text = f"🎫 알바 기회: **{charges}/{MAX_JOB_CHARGES}**"
        if charges < MAX_JOB_CHARGES and remaining > 0:
            charge_text += f"\n⏳ 다음 기회 충전: **{format_seconds(remaining)}**"
        elif charges >= MAX_JOB_CHARGES:
            charge_text += "\n🟢 기회 풀충전 (추가 충전 없음)"

        embed = discord.Embed(
            title="🧑‍💼 알바",
            description=(
                f"알바를 해서 코인을 벌 수 있습니다.\n"
                f"기회는 최대 **{MAX_JOB_CHARGES}개**. 1회 사용 시 1개 소모.\n"
                f"기회가 {MAX_JOB_CHARGES}개 미만일 때만 쿨타임마다 1개 충전됩니다.\n\n"
                f"{charge_text}"
            )
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
                # 알바 기회 풀충전
                reset_job_state(interaction.user.id)

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