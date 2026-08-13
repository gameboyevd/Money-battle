import os
import asyncpg

STARTING_MONEY = 10_000
MIN_PLAYERS = 4

async def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL이 설정되지 않았습니다.")
    return await asyncpg.connect(database_url)

async def get_or_create_player(user, guild):
    if guild is None:
        raise RuntimeError("디스코드 서버에서만 사용할 수 있습니다.")
    connection = await get_db()
    try:
        return await connection.fetchrow(
            """
            INSERT INTO players (
                server_id, user_id, discord_id, username, money,
                diamonds, points, good_deed, alive, eliminated
            )
            VALUES ($1, $2, $2, $3, $4, 0, 0, 0, TRUE, FALSE)
            ON CONFLICT (server_id, discord_id)
            DO UPDATE SET username = EXCLUDED.username, updated_at = NOW()
            RETURNING *
            """,
            str(guild.id), str(user.id), user.name, STARTING_MONEY
        )
    finally:
        await connection.close()

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

async def create_waiting_game(guild, channel, host_id):
    connection = await get_db()
    try:
        return await connection.fetchrow(
            """
            INSERT INTO games (game_type, status, host_id, channel_id, current_phase, game_data)
            VALUES ('money_battle_royale', 'waiting', $1, $2, 'waiting', $3::jsonb)
            RETURNING *
            """,
            str(host_id), str(channel.id), '{"starting_money":10000,"min_players":4}'
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
        return await connection.fetchval("SELECT COUNT(*) FROM game_players WHERE game_id = $1", game_id)
    finally:
        await connection.close()

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
                    INSERT INTO games (game_type, status, host_id, channel_id, current_phase, game_data)
                    VALUES ('money_battle_royale', 'waiting', $1, $2, 'waiting', $3::jsonb)
                    RETURNING *
                    """,
                    str(user.id), str(channel.id), '{"starting_money":10000,"min_players":4}'
                )

            existing = await connection.fetchrow(
                "SELECT * FROM game_players WHERE game_id = $1 AND user_id = $2",
                game["id"], str(user.id)
            )
            if existing:
                count = await connection.fetchval("SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"])
                return game, False, count

            await connection.execute(
                """
                INSERT INTO game_players (game_id, user_id, bet_amount, result, profit)
                VALUES ($1, $2, 0, NULL, 0) ON CONFLICT (game_id, user_id) DO NOTHING
                """,
                game["id"], str(user.id)
            )
            count = await connection.fetchval("SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"])
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
                count = await connection.fetchval("SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"])
                return game, False, count

            await connection.execute("DELETE FROM game_players WHERE game_id = $1 AND user_id = $2", game["id"], str(user.id))
            count = await connection.fetchval("SELECT COUNT(*) FROM game_players WHERE game_id = $1", game["id"])
            return game, True, count
    finally:
        await connection.close()

async def start_survival_game(game_id):
    connection = await get_db()
    try:
        async with connection.transaction():
            game = await connection.fetchrow("SELECT * FROM games WHERE id = $1 FOR UPDATE", game_id)
            if not game:
                raise RuntimeError("게임을 찾을 수 없습니다.")
            if game["status"] != "waiting":
                raise RuntimeError("이미 시작되었거나 종료된 게임입니다.")

            count = await connection.fetchval("SELECT COUNT(*) FROM game_players WHERE game_id = $1", game_id)
            if count < MIN_PLAYERS:
                raise RuntimeError(f"최소 {MIN_PLAYERS}명이 필요합니다.")

            await connection.execute(
                """
                UPDATE games
                SET status = 'playing', current_phase = 'survival', started_at = NOW(),
                    game_data = jsonb_set(COALESCE(game_data, '{}'::jsonb), '{starting_money}', '10000'::jsonb, TRUE)
                WHERE id = $1 AND status = 'waiting'
                """,
                game_id
            )

            players = await connection.fetch("SELECT user_id FROM game_players WHERE game_id = $1", game_id)
            for player in players:
                await connection.execute(
                    """
                    UPDATE players
                    SET money = $1, alive = TRUE, eliminated = FALSE, good_deed = 0, updated_at = NOW()
                    WHERE user_id = $2
                    """,
                    STARTING_MONEY, str(player["user_id"])
                )
            return count
    finally:
        await connection.close()

async def create_test_game(interaction):
    await get_or_create_player(interaction.user, interaction.guild)
    connection = await get_db()
    try:
        async with connection.transaction():
            game = await connection.fetchrow(
                """
                INSERT INTO games (game_type, status, host_id, channel_id, current_phase, started_at, game_data)
                VALUES ('money_battle_royale_test', 'playing', $1, $2, 'survival_test', NOW(), $3::jsonb)
                RETURNING *
                """,
                str(interaction.user.id), str(interaction.channel.id), '{"test":true,"starting_money":10000,"min_players":1}'
            )
            await connection.execute(
                "INSERT INTO game_players (game_id, user_id, bet_amount, result, profit) VALUES ($1, $2, 0, NULL, 0)",
                game["id"], str(interaction.user.id)
            )
            await connection.execute(
                """
                UPDATE players
                SET money = $1, alive = TRUE, eliminated = FALSE, good_deed = 0, updated_at = NOW()
                WHERE user_id = $2
                """,
                STARTING_MONEY, str(interaction.user.id)
            )
            return game
    finally:
        await connection.close()
async def force_stop_game(channel_id: int):
    connection = await get_db()
    try:
        async with connection.transaction():
            # 대기 중이거나 진행 중인 게임 조회 및 락
            game = await connection.fetchrow(
                """
                SELECT * FROM games
                WHERE channel_id = $1 AND status IN ('waiting', 'playing')
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                str(channel_id)
            )
            if not game:
                return None

            # 게임 상태를 cancelled(취소됨)로 업데이트 (updated_at 컬럼 제외)
            await connection.execute(
                """
                UPDATE games
                SET status = 'cancelled', ended_at = NOW()
                WHERE id = $1
                """,
                game["id"]
            )
            return game
    finally:
        await connection.close()
