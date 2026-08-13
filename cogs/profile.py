import discord
from discord import app_commands
from discord.ext import commands

from database import get_or_create_player

class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="프로필", description="내 게임 정보(코인, 다이아, 선행 포인트)를 확인합니다.")
    async def profile(self, interaction: discord.Interaction):
        # 유저 DB 데이터 조회 또는 생성
        player = await get_or_create_player(interaction.user, interaction.guild)
        
        embed = discord.Embed(
            title=f"👤 {interaction.user.display_name} 님의 내 정보",
            description="현재 보유한 재화 및 포인트 정보입니다.",
            color=0xf39c12
        )
        
        # 아바타 이미지가 있을 경우 프로필 썸네일 설정
        if interaction.user.display_avatar:
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # 주요 재화 표시
        embed.add_field(name="🪙 보유 코인", value=f"**{player['money']:,}** 코인", inline=False)
        embed.add_field(name="💎 보유 다이아", value=f"**{player.get('diamond', 0):,}** 개", inline=False)
        embed.add_field(name="😇 선행 포인트", value=f"**{player.get('good_deed', 0):,}** P", inline=False)
        
        embed.set_footer(text="머니 배틀로얄 • /다이아상점 을 통해 시작 자금을 강화해보세요!")

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
