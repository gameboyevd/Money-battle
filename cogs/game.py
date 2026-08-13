import asyncio
import random
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


# ==========================================
# 🎨 EMBED & UI HELPER FUNCTIONS
# ==========================================

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
        f"💎 **보유 다이아:** {player_data.get('diamond', 0):,}개\n"
        f"😇 **선행 포인트:** {player_data.get('good_deed', 0):,} P\n\n"
        "☠️ **다음 탈락 판정까지 준비 중...**\n\n"
        "게임에서 돈을 벌고 최후의 1인이 되어보세요!\n\n"
        f"`{status_text}`"
    )
    return embed


# ==========================================
# 📚 상세 통합 설명서 (PAGINATION VIEW)
# ==========================================

class HelpGuideView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.category = "rule"
        self.page = 0
        self.pages = {
            "rule": [
                discord.Embed(
                    title="📖 머니 배틀로얄 - 기본 규칙 (1/2)",
                    description=(
                        "💰 **게임 목표**\n"
                        "다양한 미니게임, 알바, 배팅을 통해 코인을 벌고, 주기마다 찾아오는 **최저 코인 보유자 탈락 판정**에서 살아남아 최후의 1인이 되세요!\n\n"
                        "⚙️ **주요 구조**\n"
                        "• **시작 자금:** 기본 10,000 코인 (다이아 상점으로 증액 가능)\n"
                        "• **탈락 주기:** 설정된 시간(1분~60분)마다 코인이 가장 적은 플레이어가 탈락합니다.\n"
                        "• **탈락 코인:** 탈락자의 남아있는 코인은 **JACKPOT 풀**로 이동합니다.\n"
                        "• **재화 초기화:** 게임 종료 시 보유 코인과 선행 포인트는 초기화됩니다."
                    ),
                    color=0x3498db
                ),
                discord.Embed(
                    title="📖 머니 배틀로얄 - 재화 & 기부 (2/2)",
                    description=(
                        "🪙 **코인:** 서바이벌 핵심 재화 (게임/알바/상점 이용, 게임 후 초기화)\n"
                        "💎 **다이아:** 게임 종료 후에도 영구 유지되는 재화 (시작 자금 강화에 사용)\n"
                        "😇 **선행 포인트:** 꼴등에게 코인을 기부하면 획득 (천사의 상점에서 전용 아이템 구매)\n\n"
                        "❤️ **기부 시스템**\n"
                        "현재 최하위 플레이어에게 코인을 기부하여 선행 포인트를 얻고 생존시킬 수 있습니다."
                    ),
                    color=0x3498db
                )
            ],
            "game": [
                discord.Embed(
                    title="🎮 미니게임 상세 설명 (1/3)",
                    description=(
                        "🃏 **블랙잭 / 마스터 블랙잭**\n"
                        "• 21을 넘지 않고 딜러보다 높은 숫자를 만드는 게임.\n"
                        "• K/Q/J=10점, A=1 또는 11점.\n"
                        "• `HIT`, `STAND`, `DOUBLE`(배팅2배+1장), `SURRENDER`(포기 후 50% 환불) 조작 가능.\n\n"
                        "🃏 **에이스 브레이커**\n"
                        "• 3장의 카드로 [높음 ➔ 낮음 ➔ 높음] 순서대로 비교하여 승부하는 심리전.\n"
                        "• **🅰️ ACE:** 모든 숫자 카드를 이김 (보유 수에 따라 최대 3.1배 보너스).\n"
                        "• **🃏 JOKER:** ACE를 파괴하고 승리 (성공 시 2.1배, 사용 시 소멸)."
                    ),
                    color=0x2ecc71
                ),
                discord.Embed(
                    title="🎲 미니 친치로 전체 족보 (2/3)",
                    description=(
                        "주사위 3개를 굴려 승부합니다. (부모/자식 승부)\n\n"
                        "🎯 **핀조로 (1, 1, 1):** 배팅액의 **10배**\n"
                        "🎯 **고조로 (5, 5, 5):** 배팅액의 **5배**\n"
                        "🌪️ **아라시 (같은 숫자 3개):** 배팅액의 **3배** (핀조로/고조로 제외)\n"
                        "⚡ **시고로 (4, 5, 6):** 배팅액의 **2배**\n"
                        "🎲 **기본 족보 (같은 숫자 2개 + 다른 숫자 1개):**\n"
                        "  └ 예: 6 / 3 / 6 ➔ **눈 3** (남은 숫자 1개가 점수, 높은 쪽 승리)\n"
                        "👑 **즉시 승리:** 부모 굴림 6 / 자식 굴림 1 나오면 즉시 승리\n"
                        "💀 **히후미 (1, 2, 3):** 즉시 패배 및 **배팅액 2배 손실**"
                    ),
                    color=0x2ecc71
                ),
                discord.Embed(
                    title="🎡 룰렛 / 🎭 야바위 / 기타 (3/3)",
                    description=(
                        "🎡 **룰렛 배율 & 상세 구간**\n"
                        "• **홀 / 짝:** x1.85 | **레드 / 블랙:** x1.85\n"
                        "• **1~18 / 19~36 구간:** x1.85\n"
                        "• **3분할 구간 (12개 숫자):** 1st(1~12), 2nd(13~24), 3rd(25~36) ➔ **x2.7**\n"
                        "• **6개 숫자:** x5.5 | **3개 숫자:** x10 | **1개 숫자 (단독):** x30\n"
                        "• *(초록색 0칸 존재)*\n\n"
                        "💣 **폭탄 룰렛:** 1.15배 ➔ 1.5배 ➔ 2.4배 ➔ 5.28배 ➔ 21.12배 (폭탄 시 전액 몰수)\n\n"
                        "🎭 **야바위**\n"
                        "• 시작 전 **난이도 1~5 선택 가능**\n"
                        "• 난이도가 올라갈수록 **컵의 개수가 늘어남** (성공 시 보상 대폭 증가)\n\n"
                        "🧠 **인디언 포커** / 🔢 **홀짝** / 🏇 **경마** / 🎟️ **복권 시스템**"
                    ),
                    color=0x2ecc71
                )
            ],
            "job": [
                discord.Embed(
                    title="🧑‍💼 알바 상세 설명 (1/1)",
                    description=(
                        "참가비나 손실 위험이 없는 노리스크 코인 수급 수단입니다. (쿨타임: 5분)\n\n"
                        "🧹 **청소 (20,000):** 화면 오염물 빠르게 클릭\n"
                        "📦 **택배 (22,000):** 주소 확인 후 상자 분류\n"
                        "🎯 **과녁 (22,000):** 제한시간 내 과녁 클릭\n"
                        "🍔 **패스트푸드 (25,000):** 주문 재료 순서대로 조리\n"
                        "🏃 **배달 (25,000):** 지정 경로 순서대로 방문\n"
                        "🍳 **주방 (28,000):** 레시피 기억 후 요리\n"
                        "🧠 **데이터 입력 (30,000):** 제시된 문자/숫자 정확히 입력\n"
                        "🎣 **낚시 (30,000):** 타이밍 맞춰 버튼 누르기"
                    ),
                    color=0xf1c40f
                )
            ],
            "item": [
                discord.Embed(
                    title="🎒 일반 상점 아이템 (1/2)",
                    description=(
                        "⚠️ **공통 규칙:** 게임 시작 전 **10초 대기 시간**에만 사용 가능.\n\n"
                        "👁️ **정찰권 (300,000):** 상대 ACE 보유 여부, 인디언 포커 힌트, 룰렛 확률 확인.\n"
                        "🪣 **빨대 쪼옵 (600,000):** 지정 상대의 코인 중 소액 강제 흡수.\n"
                        "🎭 **밑장빼기권 (800,000):** 게임 시작 전 내 패 1장 랜덤 교체.\n"
                        "⏳ **시간 연장권 (1,000,000):** 다음 탈락 판정 3분 연장 (중복 불가).\n"
                        "🎟️ **경매 주최권 (1,500,000):** 10초 후 미스터리 상자 실시간 경매 개최."
                    ),
                    color=0xe74c3c
                ),
                discord.Embed(
                    title="😇 천사의 상점 아이템 (2/2)",
                    description=(
                        "⚠️ **공통 규칙:** 게임 중 선행 포인트로 구매하며, **한 게임당 단 1개만 구매 가능!**\n\n"
                        "🪽 **천사의 구원 (3,000,000P):** 탈락 대상 지정 시 1회 면제.\n"
                        "🏹 **큐피트 소환권 (6,000,000P):** 꼴등과 운명 연결. 꼴등 탈락 시 자신도 동반 탈락.\n"
                        "⏳ **신의 모래시계 (7,000,000P):** 직전 배팅으로 잃은 코인 1회 완전 복구.\n"
                        "🕊️ **천사의 구제 (8,000,000P):** 상위권 코인 일부를 뺏어 하위권 분배.\n"
                        "🏛️ **승천궁 (10,000,000P):** 탈락자 1명 임시 부활 및 연동 타격."
                    ),
                    color=0xe74c3c
                )
            ]
        }

    def get_current_embed(self):
        return self.pages[self.category][self.page]

    def update_buttons(self):
        max_pages = len(self.pages[self.category])
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page >= max_pages - 1)

    @discord.ui.button(label="📜 기본 규칙", style=discord.ButtonStyle.primary, row=0)
    async def cat_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.category = "rule"
        self.page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    @discord.ui.button(label="🎮 게임 설명", style=discord.ButtonStyle.primary, row=0)
    async def cat_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.category = "game"
        self.page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    @discord.ui.button(label="🧑‍💼 알바 설명", style=discord.ButtonStyle.primary, row=0)
    async def cat_job(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.category = "job"
        self.page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    @discord.ui.button(label="🎒 아이템 설명", style=discord.ButtonStyle.primary, row=0)
    async def cat_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.category = "item"
        self.page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    @discord.ui.button(label="◀ 이전 페이지", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    @discord.ui.button(label="다음 페이지 ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < len(self.pages[self.category]) - 1:
            self.page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_current_embed(), view=self)


# ==========================================
# 💎 다이아 상점 VIEW (시작 전 전용)
# ==========================================

class DiamondShopView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(label="+50,000 시작자금 (💎 10개)", style=discord.ButtonStyle.success, row=0)
    async def buy_boost_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("💎 [다이아 상점] 시작 자금 +50,000 코인 추가 구매가 완료되었습니다!", ephemeral=True)

    @discord.ui.button(label="+200,000 시작자금 (💎 35개)", style=discord.ButtonStyle.success, row=0)
    async def buy_boost_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("💎 [다이아 상점] 시작 자금 +200,000 코인 추가 구매가 완료되었습니다!", ephemeral=True)


# ==========================================
# 🎮 SURVIVAL GAME & WAITING VIEWS
# ==========================================

class SurvivalGameView(discord.ui.View):
    def __init__(self, game_id: int):
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
                "🃏 블랙잭 | 👑 마스터 블랙잭\n🃏 에이스 브레이커 | 🎲 미니 친치로\n"
                "🧠 인디언 포커 | 🎡 룰렛\n💣 폭탄 룰렛 | 🔢 홀짝\n🎭 야바위 | 🏇 경마 | 🎰 슬롯",
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
        await interaction.response.send_message("🏪 **일반 상점 & 천사의 상점** 준비 중입니다.", ephemeral=True)

    @discord.ui.button(label="기부", emoji="😇", style=discord.ButtonStyle.secondary, row=1)
    async def donate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("😇 **꼴등 살리기 기부 기능** 준비 중입니다.", ephemeral=True)

    @discord.ui.button(label="아이템", emoji="🎒", style=discord.ButtonStyle.secondary, row=1)
    async def items(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🎒 **아이템 가방** 준비 중입니다.", ephemeral=True)

    @discord.ui.button(label="내 정보", emoji="👤", style=discord.ButtonStyle.secondary, row=2)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_or_create_player(interaction.user, interaction.guild)
        await interaction.response.send_message(
            f"👤 **{interaction.user.display_name} 님의 프로필**\n\n"
            f"🪙 보유 코인: **{player['money']:,} 코인**\n"
            f"💎 보유 다이아: **{player.get('diamond', 0):,} 개**\n"
            f"😇 선행 포인트: **{player.get('good_deed', 0):,} P**",
            ephemeral=True
        )


class WaitingView(discord.ui.View):
    def __init__(self, game_id):
        super().__init__(timeout=300)
        self.game_id = game_id

    async def interaction_check(self, interaction: discord.Interaction):
        try:
            game = await get_waiting_game(interaction.channel.id)
            if not game or game["id"] != self.game_id:
                await interaction.response.send_message("🔒 현재 대기 중인 게임이 없습니다.", ephemeral=True)
                return False
            return True
        except Exception:
            await interaction.response.send_message("🔴 게임 상태를 확인할 수 없습니다.", ephemeral=True)
            return False

    @discord.ui.button(label="게임 참가", emoji="🎮", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)

        async with lock:
            game, joined, count = await join_game_player(interaction.user, interaction.guild, interaction.channel)
            if not joined:
                return await interaction.response.send_message(f"⚠️ 이미 참가 중입니다. (현재: {count}명)", ephemeral=True)
            await interaction.response.send_message(f"🎉 참가 완료! (현재 참가자: {count}명 / 최소: {MIN_PLAYERS}명)", ephemeral=True)

    @discord.ui.button(label="참가 취소", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 처리 중입니다.", ephemeral=True)

        async with lock:
            game, cancelled, count = await cancel_game_player(interaction.user, interaction.guild, interaction.channel)
            if not cancelled:
                return await interaction.response.send_message(f"⚠️ 참가 중이 아닙니다.", ephemeral=True)
            await interaction.response.send_message(f"❌ 참가를 취소했습니다. (현재 참가자: {count}명)", ephemeral=True)

    @discord.ui.button(label="게임 시작", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock = action_lock(interaction.user.id)
        if lock.locked():
            return await interaction.response.send_message("⏳ 이전 요청을 처리하고 있습니다.", ephemeral=True)

        async with lock:
            game = await get_waiting_game(interaction.channel.id)
            if str(game["host_id"]) != str(interaction.user.id):
                return await interaction.response.send_message("🔒 방장만 게임을 시작할 수 있습니다.", ephemeral=True)

            count = await get_player_count(game["id"])
            if count < MIN_PLAYERS:
                return await interaction.response.send_message(f"⚠️ 최소 {MIN_PLAYERS}명이 필요합니다. (현재: {count}명)", ephemeral=True)

            await interaction.response.send_message("🎮 **3초 후 서바이벌 게임이 시작됩니다!**")
            for number in range(3, 0, -1):
                await asyncio.sleep(1)
                await interaction.edit_original_response(content=f"🎮 **MONEY BATTLE ROYALE**\n\n🔥 게임 시작까지 **{number}초**!")

            started_count = await start_survival_game(game["id"])
            player = await get_or_create_player(interaction.user, interaction.guild)
            embed = create_game_embed(game["id"], player, alive_count=started_count, rank=1)

            await interaction.edit_original_response(content=None, embed=embed, view=SurvivalGameView(game["id"]))


# ==========================================
# ⚙️ MAIN GAME COG & COMMANDS
# ==========================================

class GameCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="메인", description="게임 대기방을 개설하거나 현재 서바이벌 메인 패널을 엽니다.")
    async def main_menu(self, interaction: discord.Interaction):
        connection = await get_db()
        try:
            game = await connection.fetchrow(
                "SELECT * FROM games WHERE channel_id = $1 AND status IN ('waiting', 'playing') ORDER BY id DESC LIMIT 1",
                str(interaction.channel.id)
            )
        finally:
            await connection.close()

        if not game:
            game = await get_or_create_waiting_game(interaction.guild, interaction.channel, interaction.user.id)
            view = WaitingView(game["id"])
            await interaction.response.send_message("🎮 **Money Battle Royale 대기방이 생성되었습니다!**", view=view)
        elif game["status"] == "waiting":
            view = WaitingView(game["id"])
            await interaction.response.send_message("🎮 **Money Battle Royale 대기방**", view=view)
        else:
            player = await get_or_create_player(interaction.user, interaction.guild)
            alive_count = await get_player_count(game["id"])
            embed = create_game_embed(game["id"], player, alive_count=alive_count)
            await interaction.response.send_message(embed=embed, view=SurvivalGameView(game["id"]))

    @app_commands.command(name="다이아상점", description="게임 시작 전 다이아를 사용해 시작 조건(초기 자금)을 강화합니다.")
    async def diamond_shop(self, interaction: discord.Interaction):
        connection = await get_db()
        try:
            game = await connection.fetchrow(
                "SELECT * FROM games WHERE channel_id = $1 AND status = 'playing'",
                str(interaction.channel.id)
            )
        finally:
            await connection.close()

        if game:
            return await interaction.response.send_message("❌ 게임이 시작된 후에는 다이아 상점을 이용할 수 없습니다.", ephemeral=True)

        embed = discord.Embed(
            title="💎 다이아 상점",
            description="다이아를 사용하여 다음 서바이벌 게임의 **시작 자금**을 크게 늘릴 수 있습니다.\n\n"
                        "• **+50,000 코인:** 💎 10개\n"
                        "• **+200,000 코인:** 💎 35개",
            color=0x00ffff
        )
        await interaction.response.send_message(embed=embed, view=DiamondShopView(interaction.user.id), ephemeral=True)

    @app_commands.command(name="설명서", description="머니 배틀로얄의 규칙, 게임, 알바, 아이템 설명을 확인합니다.")
    async def guide(self, interaction: discord.Interaction):
        view = HelpGuideView()
        await interaction.response.send_message(embed=view.get_current_embed(), view=view, ephemeral=True)

    @app_commands.command(name="테스트게임", description="테스트용 대기방을 바로 생성하고 진행합니다.")
    async def test_game(self, interaction: discord.Interaction):
        game = await create_test_game(interaction)
        player = await get_or_create_player(interaction.user, interaction.guild)
        embed = create_game_embed(game["id"], player, alive_count=1, rank=1, is_test=True)
        await interaction.response.send_message(embed=embed, view=SurvivalGameView(game["id"]))

    @app_commands.command(name="강제종료", description="현재 채널의 게임을 강제 종료합니다 (관리자 전용).")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_stop(self, interaction: discord.Interaction):
        stopped_game = await force_stop_game(interaction.channel.id)
        if not stopped_game:
            return await interaction.response.send_message("❌ 진행 중인 게임이 없습니다.", ephemeral=True)
        await interaction.response.send_message(f"🛑 게임(ID: {stopped_game['id']})이 강제 종료되었습니다.")


async def setup(bot):
    await bot.add_cog(GameCog(bot))
