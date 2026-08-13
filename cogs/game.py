import asyncio
import discord
from discord.ext import commands

from database import (
    get_db, get_waiting_game, join_game_player,
    cancel_game_player, get_player_count, start_survival_game,
    get_or_create_waiting_game, get_or_create_player
)
from utils import MIN_PLAYERS, STARTING_MONEY, action_lock, JOBS, get_job_remaining, format_seconds
from cogs.job import JobView


class WaitingView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=300)
        self.game_id = game_id

    async def interaction_check(self, interaction: discord.Interaction):
        game = await get_waiting_game(interaction.channel.id)
        if not game or game["id"] != self.game_id:
            await interaction.response.send_message("🔒 사용 불가능한 대기방입니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="게임 참가", emoji="🎮", style=discord.ButtonStyle.success)
    async def join(self, interaction, button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다...", ephemeral=True)

        async with lock:
            game, joined, count = await join_game_player(interaction.user, interaction.guild, interaction.channel)
            if not joined:
                return await interaction.response.send_message(f"⚠️ 이미 참가 중입니다. (현재 {count}명)", ephemeral=True)
            await interaction.response.send_message(f"🎉 게임 참가 완료! (현재 {count}명 / 최소 {MIN_PLAYERS}명)", ephemeral=True)

    @discord.ui.button(label="참가 취소", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다...", ephemeral=True)

        async with lock:
            game, cancelled, count = await cancel_game_player(interaction.user, interaction.guild, interaction.channel)
            if not cancelled:
                return await interaction.response.send_message(f"⚠️ 참가 중이 아닙니다.", ephemeral=True)
            await interaction.response.send_message(f"❌ 참가 취소완료. (현재 {count}명)", ephemeral=True)

    @discord.ui.button(label="게임 시작", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start(self, interaction, button):
        lock = action_lock(interaction.user.id)
        async with lock:
            game = await get_waiting_game(interaction.channel.id)
            if str(game["host_id"]) != str(interaction.user.id):
                return await interaction.response.send_message("🔒 게임 시작은 방장만 가능합니다.", ephemeral=True)

            count = await get_player_count(game["id"])
            if count < MIN_PLAYERS:
                return await interaction.response.send_message(f"⚠️ 최소 {MIN_PLAYERS}명이 필요합니다. (현재 {count}명)", ephemeral=True)

            await interaction.response.send_message("🎮 **3초 후 게임이 시작됩니다!**")
            for number in [3, 2, 1]:
                await asyncio.sleep(1)
                await interaction.edit_original_response(content=f"🔥 게임 시작까지 **{number}초**!", view=self)

            started_count = await start_survival_game(game["id"])
            await interaction.edit_original_response(
                content=f"💰 **MONEY BATTLE ROYALE** 시작!\n👥 참가자: {started_count}명",
                view=SurvivalGameView(game["id"])
            )


class SurvivalGameView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=None)
        self.game_id = game_id

    @discord.ui.button(label="게임", emoji="🎮", style=discord.ButtonStyle.success, row=0)
    async def games(self, interaction, button):
        await interaction.response.send_message("🎮 미니게임 목록 (준비중)", ephemeral=True)

    @discord.ui.button(label="알바", emoji="🧑‍💼", style=discord.ButtonStyle.primary, row=0)
    async def jobs(self, interaction, button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        remaining = get_job_remaining(interaction.user.id)
        cooldown_text = f"⏳ 남은 쿨타임: **{format_seconds(remaining)}**" if remaining > 0 else "🟢 바로 알바 가능!"

        embed = discord.Embed(title="🧑‍💼 알바", description=f"{cooldown_text}\n\n알바 후 5분 쿨타임 적용")
        for job_name, job in JOBS.items():
            embed.add_field(name=f"{job['emoji']} {job_name}", value=f"{job['reward']:,} 코인", inline=True)
        embed.set_footer(text=f"현재 코인: {player['money']:,}")

        await interaction.response.send_message(embed=embed, view=JobView(self.game_id), ephemeral=True)


class GameCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="대기방")
    async def create_room(self, ctx):
        """대기방 생성 명령어 예시"""
        game = await get_or_create_waiting_game(ctx.guild, ctx.channel, ctx.author.id)
        view = WaitingView(game["id"])
        await ctx.send("🎮 **Money Battle Royale 대기방**", view=view)

async def setup(bot):
    await bot.add_cog(GameCog(bot))
