import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_waiting_game, join_game_player, cancel_game_player,
    get_player_count, start_survival_game, get_or_create_waiting_game,
    get_or_create_player, create_test_game, force_stop_game, get_db
)
from utils import MIN_PLAYERS, STARTING_MONEY, action_lock, JOBS, get_job_remaining, format_seconds
from cogs.job import JobView


# 메인 화면 임베드 생성 함수
def create_game_embed(game_id: int, player_data: dict, alive_count: int = 1, rank: int = 1, is_test: bool = False):
    embed = discord.Embed(color=0x2b2d31)
    
    status_text = f"Game ID: {game_id}" + (" • TEST MODE" if is_test else "")
    
    embed.description = (
        "💰 **MONEY BATTLE ROYALE**\n"
        "-----------------------------------\n"
        "🔥 **SURVIVAL GAME**\n"
        "-----------------------------------\n\n"
        f"👥 **생존자:** {alive_count}명\n"
        f"🏆 **현재 순위:** {rank}위\n"
        f"🪙 **보유 코인:** {player_data['money']:,}\n"
        f"😇 **선행 포인트:** {player_data.get('good_deed', 0)}\n\n"
        "☠️ **다음 탈락 판정까지 준비 중...**\n\n"
        "게임에서 돈을 벌고\n"
        "최후의 1인이 되어보세요!\n\n"
        f"`{status_text}`"
    )
    return embed


class SurvivalGameView(discord.ui.View):
    def __init__(self, game_id: int):
        super().__init__(timeout=None)
        self.game_id = game_id

    # 1행 버튼
    @discord.ui.button(label="게임", emoji="🎮", style=discord.ButtonStyle.success, row=0)
    async def games(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 이전 요청을 처리하고 있습니다.", ephemeral=True)

        async with lock:
            await interaction.response.send_message(
                "🎮 **게임 메뉴**\n\n"
                "🃏 블랙잭\n🃏 에이스 브레이커\n🎲 미니 친치로\n🧠 인디언 포커\n"
                "🎡 룰렛\n💣 폭탄 룰렛\n🔢 홀짝\n🎭 야바위\n🏇 경마\n\n"
                "⚠️ 게임 기능은 순차적으로 연결됩니다.",
                ephemeral=True
            )

    @discord.ui.button(label="알바", emoji="🧑‍💼", style=discord.ButtonStyle.primary, row=0)
    async def jobs(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 이전 요청을 처리하고 있습니다.", ephemeral=True)

        async with lock:
            player = await get_or_create_player(interaction.user, interaction.guild)
            remaining = get_job_remaining(interaction.user.id)
            cooldown_text = f"⏳ 현재 알바 쿨타임: **{format_seconds(remaining)}**" if remaining > 0 else "🟢 지금 바로 알바할 수 있습니다."

            embed = discord.Embed(
                title="🧑‍💼 알바",
                description=f"알바를 해서 코인을 벌 수 있습니다.\n\n{cooldown_text}\n\n한 번 일을 하면 **5분 동안** 다시 일할 수 없습니다."
            )
            for job_name, job in JOBS.items():
                embed.add_field(name=f"{job['emoji']} {job_name}", value=f"{job['reward']:,} 코인", inline=True)
            embed.set_footer(text=f"현재 코인: {player['money']:,}")

            await interaction.response.send_message(embed=embed, view=JobView(self.game_id), ephemeral=True)

    @discord.ui.button(label="상점", emoji="🏪", style=discord.ButtonStyle.secondary, row=0)
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🏪 상점 기능 준비 중입니다.", ephemeral=True)

    # 2행 버튼
    @discord.ui.button(label="기부", emoji="😇", style=discord.ButtonStyle.secondary, row=1)
    async def donate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("😇 기부 기능 준비 중입니다.", ephemeral=True)

    @discord.ui.button(label="아이템", emoji="🎒", style=discord.ButtonStyle.secondary, row=1)
    async def items(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🎒 아이템 가방 준비 중입니다.", ephemeral=True)

    # 3행 버튼
    @discord.ui.button(label="내 정보", emoji="👤", style=discord.ButtonStyle.secondary, row=2)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        await interaction.response.send_message(
            f"👤 **내 정보**\n\n"
            f"💰 보유 코인: **{player['money']:,} 코인**\n"
            f"😇 선행 포인트: **{player.get('good_deed', 0)} P**",
            ephemeral=True
        )


class WaitingView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=300)
        self.game_id = game_id

    async def interaction_check(self, interaction: discord.Interaction):
        try:
            game = await get_waiting_game(interaction.channel.id)
            if not game:
                await interaction.response.send_message("🔒 현재 대기 중인 게임이 없습니다.", ephemeral=True)
                return False
            if game["id"] != self.game_id:
                await interaction.response.send_message("🔒 이 버튼은 현재 게임에 사용할 수 없습니다.", ephemeral=True)
                return False
            return True
        except Exception as e:
            await interaction.response.send_message("🔴 게임 상태를 확인할 수 없습니다.", ephemeral=True)
            return False

    @discord.ui.button(label="게임 참가", emoji="🎮", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)

        async with lock:
            try:
                game = await get_waiting_game(interaction.channel.id)
                if not game or game["id"] != self.game_id:
                    return await interaction.response.send_message("⚠️ 이 대기방은 더 이상 사용할 수 없습니다.", ephemeral=True)

                game, joined, count = await join_game_player(interaction.user, interaction.guild, interaction.channel)
                if not joined:
                    return await interaction.response.send_message(f"⚠️ 이미 참가 중입니다.\n\n👥 현재 참가자: **{count}명**", ephemeral=True)

                await interaction.response.send_message(
                    f"🎉 게임 참가 완료!\n\n👥 현재 참가자: **{count}명**\n🎯 최소 참가 인원: **{MIN_PLAYERS}명**", ephemeral=True
                )
            except Exception as e:
                await interaction.response.send_message(f"🔴 참가 실패\n`{type(e).__name__}`\n{str(e)[:300]}", ephemeral=True)

    @discord.ui.button(label="참가 취소", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)

        async with lock:
            try:
                game_check = await get_waiting_game(interaction.channel.id)
                if not game_check or game_check["id"] != self.game_id:
                    return await interaction.response.send_message("🔒 현재 참가 취소가 가능한 대기 게임이 없습니다.", ephemeral=True)

                game, cancelled, count = await cancel_game_player(interaction.user, interaction.guild, interaction.channel)
                if game is None or not cancelled:
                    return await interaction.response.send_message(f"⚠️ 참가 중이 아닙니다.\n\n👥 현재 참가자: **{count}명**", ephemeral=True)

                await interaction.response.send_message(f"❌ 참가를 취소했습니다.\n\n👥 현재 참가자: **{count}명**", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"🔴 참가 취소 중 오류가 발생했습니다.\n`{type(e).__name__}`", ephemeral=True)

    @discord.ui.button(label="게임 시작", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 이전 요청을 처리하고 있습니다.", ephemeral=True)

        async with lock:
            try:
                game = await get_waiting_game(interaction.channel.id)
                if not game or game["id"] != self.game_id:
                    return await interaction.response.send_message("⚠️ 대기 중인 게임이 없습니다.", ephemeral=True)

                if str(game["host_id"]) != str(interaction.user.id):
                    return await interaction.response.send_message("🔒 게임 시작은 방장만 할 수 있습니다.", ephemeral=True)

                count = await get_player_count(game["id"])
                if count < MIN_PLAYERS:
                    return await interaction.response.send_message(
                        f"⚠️ 아직 게임을 시작할 수 없습니다.\n\n👥 현재 참가자: **{count}명**\n🎯 최소 참가자: **{MIN_PLAYERS}명**",
                        ephemeral=True
                    )

                await interaction.response.send_message("🎮 게임 시작 준비!\n\n⏳ **3초 후 게임이 시작됩니다!**")
                for number in [3, 2, 1]:
                    await asyncio.sleep(1)
                    await interaction.edit_original_response(
                        content=f"🎮 **MONEY BATTLE ROYALE**\n\n🔥 게임 시작까지 **{number}초**!", view=self
                    )

                started_count = await start_survival_game(game["id"])
                player = await get_or_create_player(interaction.user, interaction.guild)
                embed = create_game_embed(game["id"], player, alive_count=started_count, rank=1, is_test=False)

                await interaction.edit_original_response(
                    content=None,
                    embed=embed,
                    view=SurvivalGameView(game["id"])
                )
            except Exception as e:
                msg = f"🔴 게임 시작 실패\n`{type(e).__name__}`\n{str(e)[:500]}"
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)


class GameCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="대기방", description="Money Battle Royale 대기방을 생성합니다.")
    async def create_room(self, interaction: discord.Interaction):
        game = await get_or_create_waiting_game(interaction.guild, interaction.channel, interaction.user.id)
        view = WaitingView(game["id"])
        await interaction.response.send_message("🎮 **Money Battle Royale 대기방**", view=view)

    @app_commands.command(name="테스트게임", description="테스트용 게임 대기방을 바로 생성하고 시작합니다.")
    async def test_game(self, interaction: discord.Interaction):
        game = await create_test_game(interaction)
        player = await get_or_create_player(interaction.user, interaction.guild)
        embed = create_game_embed(game["id"], player, alive_count=1, rank=1, is_test=True)

        await interaction.response.send_message(
            embed=embed,
            view=SurvivalGameView(game["id"])
        )

    @app_commands.command(name="메인", description="현재 진행 중인 게임의 메인 화면 패널을 다시 엽니다.")
    async def main_menu(self, interaction: discord.Interaction):
        connection = await get_db()
        try:
            # 현재 채널에서 진행 중인 게임 조회
            game = await connection.fetchrow(
                """
                SELECT * FROM games
                WHERE channel_id = $1 AND status = 'playing'
                ORDER BY id DESC LIMIT 1
                """,
                str(interaction.channel.id)
            )
        finally:
            await connection.close()

        if not game:
            return await interaction.response.send_message(
                "❌ 현재 이 채널에서 진행 중인 게임이 없습니다.",
                ephemeral=True
            )

        player = await get_or_create_player(interaction.user, interaction.guild)
        is_test = game["game_type"] == "money_battle_royale_test"
        alive_count = await get_player_count(game["id"])

        embed = create_game_embed(game["id"], player, alive_count=alive_count, rank=1, is_test=is_test)

        await interaction.response.send_message(
            embed=embed,
            view=SurvivalGameView(game["id"])
        )

    @app_commands.command(name="강제종료", description="현재 채널에서 진행 중이거나 대기 중인 게임을 강제 종료합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_stop(self, interaction: discord.Interaction):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)

        async with lock:
            try:
                stopped_game = await force_stop_game(interaction.channel.id)
                if not stopped_game:
                    return await interaction.response.send_message(
                        "❌ 현재 채널에서 진행 중이거나 대기 중인 게임이 없습니다.",
                        ephemeral=True
                    )

                await interaction.response.send_message(
                    f"🛑 **게임 강제 종료**\n\n"
                    f"🎮 게임 ID: **{stopped_game['id']}**\n"
                    f"📌 상태: **{stopped_game['status']}** ➡️ **cancelled**\n\n"
                    f"관리자({interaction.user.mention})에 의해 게임이 강제 종료되었습니다."
                )
            except Exception as e:
                await interaction.response.send_message(
                    f"🔴 게임 강제 종료 중 오류가 발생했습니다.\n`{type(e).__name__}`: {str(e)[:300]}",
                    ephemeral=True
                )

    @force_stop.error
    async def force_stop_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("🔒 이 명령어는 **관리자 권한**이 있는 사용자만 사용할 수 있습니다.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(GameCog(bot))
