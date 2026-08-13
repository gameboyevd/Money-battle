import discord
from discord import app_commands
from discord.ext import commands
from database import get_or_create_player

class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 슬래시 명령어 등록 (@app_commands.command)
    @app_commands.command(name="프로필", description="내 프로필과 보유 코인을 확인합니다.")
    async def show_profile(self, interaction: discord.Interaction):
        player = await get_or_create_player(interaction.user, interaction.guild)
        
        embed = discord.Embed(
            title=f"👤 {interaction.user.name}님의 프로필",
            color=discord.Color.blue()
        )
        embed.add_field(name="💰 보유 코인", value=f"{player['money']:,} 코인", inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
