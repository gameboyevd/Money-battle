import discord
from discord.ext import commands
from database import get_or_create_player

class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="프로필")
    async def show_profile(self, ctx):
        player = await get_or_create_player(ctx.author, ctx.guild)
        
        embed = discord.Embed(
            title=f"👤 {ctx.author.name}님의 프로필",
            color=discord.Color.blue()
        )
        embed.add_field(name="💰 보유 코인", value=f"{player['money']:,} 코인", inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
