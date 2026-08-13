import asyncio
from datetime import datetime
from collections import defaultdict

MIN_PLAYERS = 4
STARTING_MONEY = 10_000
JOB_COOLDOWN_SECONDS = 5 * 60

job_cooldowns = {}
_user_locks = defaultdict(asyncio.Lock)

def action_lock(user_id: int):
    return _user_locks[user_id]

JOBS = {
    "청소": {"emoji": "🧹", "reward": 20_000},
    "택배": {"emoji": "📦", "reward": 22_000},
    "과녁": {"emoji": "🎯", "reward": 22_000},
    "패스트푸드": {"emoji": "🍔", "reward": 25_000},
    "배달": {"emoji": "🏃", "reward": 25_000},
    "주방": {"emoji": "🍳", "reward": 28_000},
    "데이터 입력": {"emoji": "🧠", "reward": 30_000},
    "낚시": {"emoji": "🎣", "reward": 30_000}
}

def get_job_remaining(user_id: int):
    cooldown = job_cooldowns.get(user_id)
    if cooldown is None:
        return 0
    now = datetime.utcnow()
    if now >= cooldown:
        job_cooldowns.pop(user_id, None)
        return 0
    return int((cooldown - now).total_seconds())

def format_seconds(seconds: int):
    minutes = seconds // 60
    seconds %= 60
    if minutes > 0:
        return f"{minutes}분 {seconds}초"
    return f"{seconds}초"
