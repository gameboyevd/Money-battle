import discord
from discord import app_commands
from discord.ext import commands
import database
from cogs.games.blackjack import BlackjackView, Deck, calculate_score

# ==========================================
# 💰 배팅금 입력 모달 (블랙잭용)
# ==========================================
class BetModal(discord.ui.Modal):
    def __init__(self, game_type: str):
        super().__init__(title=f"{game_type} - 배팅금 입력")
        self.game_type = game_type
        
        self.bet_input = discord.ui.TextInput(
            label="배팅할 코인 금액을 입력하세요",
            placeholder="예: 1000",
            min_length=1,
            max_length=10,
            required=True
        )
        self.add_item(self.bet_input)

    async def on_submit(self, interaction: discord.Interaction):
        # 입력값 검증
        try:
            bet = int(self.bet_input.value.strip())
            if bet <= 0:
                return await interaction.response.send_message("배팅금은 1코인 이상이어야 합니다.", ephemeral=True)
        except ValueError:
            return await interaction.response.send_message("숫자만 정확히 입력해 주세요.", ephemeral=True)

        # 유저 코인 확인
        player = await database.get_or_create_player(interaction.user, interaction.guild)
        if player['money'] < bet:
            return await interaction.response.send_message("보유 코인이 부족합니다!", ephemeral=True)

        # 🃏 블랙잭 시작 로직
        if self.game_type == "블랙잭":
            deck = Deck()
            player_hand = [deck.draw(), deck.draw()]
            dealer_hand = [deck.draw(), deck.draw()]

            p_score = calculate_score(player_hand)
            d_score = calculate_score(dealer_hand)

            # 내추럴 블랙잭 체크
            if p_score == 21:
                if d_score == 21:
                    embed = discord.Embed(title="🤝 둘 다 블랙잭! (Push)", color=0xf1c40f)
                    embed.add_field(name="🤖 딜러", value="  ".join(str(c) for c in dealer_hand))
                    embed.add_field(name=f"👤 {interaction.user.display_name}", value="  ".join(str(c) for c in player_hand))
                    embed.description = "배팅금이 환불됩니다."
                    return await interaction.response.send_message(embed=embed)
                else:
                    reward = int(bet * 1.5)
                    await database.update_player_money(interaction.user.id, interaction.guild.id, reward)
                    embed = discord.Embed(title="🔥 NATURAL BLACKJACK!", color=0x2ecc71)
                    embed.add_field(name="🤖 딜러", value="  ".join(str(c) for c in dealer_hand))
                    embed.add_field(name=f"👤 {interaction.user.display_name}", value="  ".join(str(c) for c in player_hand))
                    embed.description = f"축하합니다! 블랙잭으로 **+{reward:,} 코인** (1.5배)을 얻었습니다!"
                    return await interaction.response.send_message(embed=embed)

            # 게임 View 구동
            view = BlackjackView(None, interaction.user, interaction.guild, bet, deck, player_hand, dealer_hand)
            await interaction.response.send_message(embed=view.make_embed(), view=view)


# ==========================================
# 🎮 2단계: 게임 선택 메뉴 View
# ==========================================
class GameMenuView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=120)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인의 메인 메뉴만 이용할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🃏 블랙잭", style=discord.ButtonStyle.primary, row=0)
    async def btn_blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 블랙잭 배팅금 입력 모달 오픈
        await interaction.response.send_modal(BetModal("블랙잭"))

    @discord.ui.button(label="🎲 미니 친치로", style=discord.ButtonStyle.primary, row=0)
    async def btn_chinchiro(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("친치로 게임은 준비 중입니다! (/친치로 명령어 이용 가능)", ephemeral=True)

    @discord.ui.button(label="🎭 야바위", style=discord.ButtonStyle.primary, row=0)
    async def btn_yabawi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("야바위 게임은 준비 중입니다! (/야바위 명령어 이용 가능)", ephemeral=True)

    @discord.ui.button(label="◀ 메인으로 돌아가기", style=discord.ButtonStyle.secondary, row=1)
    async def btn_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MainMenuView(self.user)
        await interaction.response.edit_message(embed=await view.get_main_embed(interaction), view=view)


# ==========================================
# 🏠 1단계: 메인 메뉴 View
# ==========================================
class MainMenuView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=120)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인의 메인 메뉴만 이용할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def get_main_embed(self, interaction: discord.Interaction):
        player = await database.get_or_create_player(self.user, interaction.guild)
        embed = discord.Embed(
            title="🏰 머니 배틀로얄 - 메인 로비",
            description=f"환영합니다, **{self.user.display_name}**님!\n원하시는 메뉴를 아래 버튼에서 선택해 주세요.",
            color=0x3498db
        )
        embed.add_field(name="💰 보유 코인", value=f"`{player['money']:,}` 코인", inline=True)
        embed.set_thumbnail(url=self.user.display_avatar.url)
        return embed

    @discord.ui.button(label="🎮 미니게임", style=discord.ButtonStyle.success, emoji="🎲", row=0)
    async def btn_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🎮 미니게임 선택",
            description="플레이할 미니게임을 선택해 주세요!",
            color=0x2ecc71
        )
        view = GameMenuView(self.user)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🧑‍💼 알바하기", style=discord.ButtonStyle.primary, emoji="🧹", row=0)
    async def btn_job(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("알바 기능이 실행됩니다.", ephemeral=True)

    @discord.ui.button(label="🎒 상점", style=discord.ButtonStyle.secondary, emoji="🛒", row=0)
    async def btn_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("상점 기능은 준비 중입니다.", ephemeral=True)


# ==========================================
# 🚀 메인 커맨드 Cog
# ==========================================
class MainMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="메인", description="머니 배틀로얄 메인 메뉴를 열어 게임, 알바, 상점 등을 이용합니다.")
    async def main_menu(self, interaction: discord.Interaction):
        view = MainMenuView(interaction.user)
        embed = await view.get_main_embed(interaction)
        await interaction.response.send_message(embed=embed, view=view)

async def setup(bot):
    await bot.add_cog(MainMenu(bot))
