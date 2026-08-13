from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands

from database import get_db, get_or_create_player
from utils import (
    JOBS, JOB_COOLDOWN_SECONDS, job_cooldowns,
    action_lock, get_job_remaining, format_seconds
)

class JobView(discord.ui.View):
    def __init__(self, game_id=0):
        super().__init__(timeout=300)
        self.game_id = game_id

    async def interaction_check(self, interaction: discord.Interaction):
        if self.game_id == 0:
            return True

        connection = await get_db()
        try:
            game = await connection.fetchrow("SELECT * FROM games WHERE id = $1", self.game_id)
        finally:
            await connection.close()

        if not game:
            await interaction.response.send_message("🔒 이 게임은 존재하지 않습니다.", ephemeral=True)
            return False
        if game["status"] != "playing":
            await interaction.response.send_message("🔒 이 게임은 더 이상 진행 중이 아닙니다.", ephemeral=True)
            return False
        return True

    async def do_job(self, interaction: discord.Interaction, job_name: str):
        user_id = interaction.user.id
        lock = action_lock(user_id)

        if lock.locked():
            return await interaction.response.send_message("⏳ 이미 다른 요청을 처리 중입니다.", ephemeral=True)

        async with lock:
            try:
                remaining = get_job_remaining(user_id)
                if remaining > 0:
                    return await interaction.response.send_message(
                        f"⏳ **아직 알바를 할 수 없습니다.**\n\n🕐 남은 시간: **{format_seconds(remaining)}**",
                        ephemeral=True
                    )

                job = JOBS[job_name]
                await get_or_create_player(interaction.user, interaction.guild)

                connection = await get_db()
                try:
                    new_money = await connection.fetchval(
                        """
                        UPDATE players
                        SET money = money + $1, updated_at = NOW()
                        WHERE server_id = $2 AND user_id = $3
                        RETURNING money
                        """,
                        job["reward"], str(interaction.guild.id), str(interaction.user.id)
                    )
                finally:
                    await connection.close()

                job_cooldowns[user_id] = datetime.utcnow() + timedelta(seconds=JOB_COOLDOWN_SECONDS)

                await interaction.response.send_message(
                    f"{job['emoji']} **{job_name} 알바 완료!**\n\n"
                    f"💰 획득: **+{job['reward']:,} 코인**\n"
                    f"🪙 현재 코인: **{new_money:,} 코인**\n\n"
                    "⏳ 다음 알바까지 **5분**",
                    ephemeral=True
                )
            except Exception as e:
                msg = f"🔴 알바 처리 오류\n`{type(e).__name__}`\n{str(e)[:300]}"
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="청소", emoji="🧹", style=discord.ButtonStyle.primary, row=0)
    async def cleaning(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "청소")

    @discord.ui.button(label="택배", emoji="📦", style=discord.ButtonStyle.primary, row=0)
    async def package(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "택배")

    @discord.ui.button(label="과녁", emoji="🎯", style=discord.ButtonStyle.primary, row=0)
    async def target(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "과녁")

    @discord.ui.button(label="패스트푸드", emoji="🍔", style=discord.ButtonStyle.primary, row=1)
    async def fast_food(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "패스트푸드")

    @discord.ui.button(label="배달", emoji="🏃", style=discord.ButtonStyle.primary, row=1)
    async def delivery(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "배달")

    @discord.ui.button(label="주방", emoji="🍳", style=discord.ButtonStyle.primary, row=1)
    async def kitchen(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "주방")

    @discord.ui.button(label="데이터 입력", emoji="🧠", style=discord.ButtonStyle.primary, row=2)
    async def data_input(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "데이터 입력")

    @discord.ui.button(label="낚시", emoji="🎣", style=discord.ButtonStyle.primary, row=2)
    async def fishing(self, interaction: discord.Interaction, button: discord.ui.Button): await self.do_job(interaction, "낚시")

    @discord.ui.button(label="닫기", emoji="❌", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🧑‍💼 알바 메뉴를 닫았습니다.", embed=None, view=None)


# cogs/job.py 파일의 JobCog 수정
class JobCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # @app_commands.command(name="알바") 부분 삭제됨

async def setup(bot):
    await bot.add_cog(JobCog(bot))

