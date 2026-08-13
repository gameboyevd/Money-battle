import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_waiting_game, join_game_player, cancel_game_player,
    get_player_count, start_survival_game, get_or_create_waiting_game,
    get_or_create_player, create_test_game
)
from utils import MIN_PLAYERS, STARTING_MONEY, action_lock, JOBS, get_job_remaining, format_seconds
from cogs.job import JobView

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
                await interaction.edit_original_response(
                    content=(
                        f"💰 **MONEY BATTLE ROYALE**\n\n🎉 **게임이 시작되었습니다!**\n\n"
                        f"👥 참가자: **{started_count}명**\n🪙 시작 자금: **{STARTING_MONEY:,} 코인**\n\n"
                        "☠️ 이제 서바이벌이 시작됩니다."
                    ),
                    view=SurvivalGameView(game["id"])
                )
            except Exception as e:
                msg = f"🔴 게임 시작 실패\n`{type(e).__name__}`\n{str(e)[:500]}"
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)


class SurvivalGameView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=None)
        self.game_id = game_id

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
        await interaction.response.send_message(
            f"🧪 **테스트 게임 시작!** (ID: {game['id']})\n\n버튼을 눌러 미니게임 및 알바 테스트를 진행해보세요.",
            view=SurvivalGameView(game["id"])
        )

async def setup(bot):
    await bot.add_cog(GameCog(bot))
