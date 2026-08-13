import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import database  # 기존 Supabase 연동 모듈

# 카드 덱 생성 클래스
SUITS = ['♠️', '♥️', '♦️', '♣️']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']

class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank

    def __str__(self):
        return f"{self.suit} `{self.rank}`"

    @property
    def value(self):
        if self.rank in ['J', 'Q', 'K']:
            return 10
        elif self.rank == 'A':
            return 11  # 계산 함수에서 1로 변환 처리
        return int(self.rank)

class Deck:
    def __init__(self, count=6): # 카지노 표준 6덱
        self.cards = [Card(s, r) for _ in range(count) for s in SUITS for r in RANKS]
        random.shuffle(self.cards)

    def draw(self):
        return self.cards.pop()

def calculate_score(hand):
    score = sum(card.value for card in hand)
    aces = sum(1 for card in hand if card.rank == 'A')
    
    # A카드를 11점 ➔ 1점으로 유연하게 조정
    while score > 21 and aces > 0:
        score -= 10
        aces -= 1
    return score

# 블랙잭 인터랙티브 UI 버튼 View
class BlackjackView(discord.ui.View):
    def __init__(self, cog, user, guild, bet, deck, player_hand, dealer_hand):
        super().__init__(timeout=60)
        self.cog = cog
        self.user = user
        self.guild = guild
        self.bet = bet
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.double_down = False
        self.surrendered = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인의 게임만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    def make_embed(self, hide_dealer=True, result_title=None, result_color=0x3498db):
        embed = discord.Embed(
            title=result_title or "🃏 마스터 블랙잭 (Blackjack)",
            color=result_color
        )
        
        # 딜러 핸드
        if hide_dealer:
            dealer_text = f"{self.dealer_hand[0]}  🂠 `[ 덮임 ]`"
            dealer_score = "?"
        else:
            dealer_text = "  ".join(str(c) for c in self.dealer_hand)
            dealer_score = str(calculate_score(self.dealer_hand))

        embed.add_field(name=f"🤖 딜러 [점수: {dealer_score}]", value=dealer_text, inline=False)
        
        # 플레이어 핸드
        player_text = "  ".join(str(c) for c in self.player_hand)
        p_score = calculate_score(self.player_hand)
        embed.add_field(name=f"👤 {self.user.display_name} [점수: {p_score}]", value=player_text, inline=False)
        
        embed.set_footer(text=f"현재 배팅금: {self.bet:,} 코인")
        return embed

    def disable_all_buttons(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="히트 (Hit)", style=discord.ButtonStyle.primary, emoji="➕")
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 첫 턴 이후 더블다운/서렌더 버튼 비활성화
        self.double_btn.disabled = True
        self.surrender_btn.disabled = True

        self.player_hand.append(self.deck.draw())
        score = calculate_score(self.player_hand)

        if score > 21:  # 버스트(Bust)
            self.disable_all_buttons()
            await database.update_player_money(self.user.id, self.guild.id, -self.bet)
            embed = self.make_embed(
                hide_dealer=False, 
                result_title="💥 버스트! (21 초과 패배)", 
                result_color=0xe74c3c
            )
            embed.description = f"💸 **-{self.bet:,} 코인**을 잃었습니다."
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()
        else:
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="스탠드 (Stand)", style=discord.ButtonStyle.success, emoji="✋")
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.disable_all_buttons()
        await self.finish_game(interaction)

    @discord.ui.button(label="더블다운 (Double)", style=discord.ButtonStyle.danger, emoji="✖️")
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 자금 부족 확인
        player = await database.get_or_create_player(self.user, self.guild)
        if player['money'] < self.bet * 2:
            return await interaction.response.send_message("더블다운을 위한 추가 코인이 부족합니다!", ephemeral=True)

        self.bet *= 2
        self.player_hand.append(self.deck.draw())
        self.disable_all_buttons()

        score = calculate_score(self.player_hand)
        if score > 21:
            await database.update_player_money(self.user.id, self.guild.id, -self.bet)
            embed = self.make_embed(
                hide_dealer=False, 
                result_title="💥 더블다운 버스트! (패배)", 
                result_color=0xe74c3c
            )
            embed.description = f"💸 **-{self.bet:,} 코인**을 잃었습니다."
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()
        else:
            await self.finish_game(interaction)

    @discord.ui.button(label="서렌더 (Surrender)", style=discord.ButtonStyle.secondary, emoji="🏳️")
    async def surrender_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.disable_all_buttons()
        refund = self.bet // 2
        loss = self.bet - refund
        await database.update_player_money(self.user.id, self.guild.id, -loss)

        embed = self.make_embed(
            hide_dealer=False, 
            result_title="🏳️ 항복 (Surrender)", 
            result_color=0x95a5a6
        )
        embed.description = f"게임에서 포기하여 배팅금의 50%를 환불받았습니다.\n💸 **-{loss:,} 코인**"
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def finish_game(self, interaction: discord.Interaction):
        # 딜러 AI: 17점 이상이 될 때까지 계속 카드를 뽑음
        while calculate_score(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.draw())

        p_score = calculate_score(self.player_hand)
        d_score = calculate_score(self.dealer_hand)

        # 결과 정산
        if d_score > 21 or p_score > d_score:
            reward = self.bet
            await database.update_player_money(self.user.id, self.guild.id, reward)
            title = "🎉 승리!"
            desc = f"**+{reward:,} 코인**을 획득했습니다!"
            color = 0x2ecc71
        elif p_score < d_score:
            await database.update_player_money(self.user.id, self.guild.id, -self.bet)
            title = "❌ 패배..."
            desc = f"💸 **-{self.bet:,} 코인**을 잃었습니다."
            color = 0xe74c3c
        else:
            title = "🤝 무승부 (Push)"
            desc = "배팅금이 환불됩니다."
            color = 0xf1c40f

        embed = self.make_embed(hide_dealer=False, result_title=title, result_color=color)
        embed.description = desc
        
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


class Blackjack(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="블랙잭", description="딜러와 21을 겨루는 정통 블랙잭 게임을 진행합니다.")
    @app_commands.describe(bet="배팅할 코인 금액")
    async def blackjack(self, interaction: discord.Interaction, bet: int):
        if bet <= 0:
            return await interaction.response.send_message("배팅금은 1 코인 이상이어야 합니다.", ephemeral=True)

        player = await database.get_or_create_player(interaction.user, interaction.guild)
        if player['money'] < bet:
            return await interaction.response.send_message("보유 코인이 부족합니다!", ephemeral=True)

        deck = Deck()
        player_hand = [deck.draw(), deck.draw()]
        dealer_hand = [deck.draw(), deck.draw()]

        p_score = calculate_score(player_hand)
        d_score = calculate_score(dealer_hand)

        # 내추럴 블랙잭(Natural Blackjack - 첫 2장 21점) 체크
        if p_score == 21:
            if d_score == 21:
                # 둘 다 블랙잭 (무승부)
                embed = discord.Embed(title="🤝 둘 다 블랙잭! (Push)", color=0xf1c40f)
                embed.add_field(name="🤖 딜러", value="  ".join(str(c) for c in dealer_hand))
                embed.add_field(name=f"👤 {interaction.user.display_name}", value="  ".join(str(c) for c in player_hand))
                embed.description = "배팅금이 환불됩니다."
                return await interaction.response.send_message(embed=embed)
            else:
                # 플레이어 단독 내추럴 블랙잭 (1.5배 보상)
                reward = int(bet * 1.5)
                await database.update_player_money(interaction.user.id, interaction.guild.id, reward)
                embed = discord.Embed(title="🔥 NATURAL BLACKJACK!", color=0x2ecc71)
                embed.add_field(name="🤖 딜러", value="  ".join(str(c) for c in dealer_hand))
                embed.add_field(name=f"👤 {interaction.user.display_name}", value="  ".join(str(c) for c in player_hand))
                embed.description = f"축하합니다! 블랙잭으로 **+{reward:,} 코인** (1.5배)을 얻었습니다!"
                return await interaction.response.send_message(embed=embed)

        view = BlackjackView(self, interaction.user, interaction.guild, bet, deck, player_hand, dealer_hand)
        await interaction.response.send_message(embed=view.make_embed(), view=view)

async def setup(bot):
    await bot.add_cog(Blackjack(bot))
