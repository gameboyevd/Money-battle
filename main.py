import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands
import asyncpg


# ==========================================
# Render HTTP 서버
# ==========================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

        self.wfile.write(
            b"Money Battle Royale Bot is running!"
        )

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(f"HTTP server started on port {port}")

    server.serve_forever()


# ==========================================
# Discord Bot
# ==========================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ==========================================
# DB 테스트
# ==========================================

async def test_database():

    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        print("DATABASE_URL is missing.")
        return

    try:
        connection = await asyncpg.connect(
            database_url
        )

        result = await connection.fetchval(
            "SELECT 1;"
        )

        await connection.close()

        if result == 1:
            print("Database connected successfully!")

    except Exception as e:
        print("Database connection failed:")
        print(type(e).__name__)
        print(str(e))


# ==========================================
# Discord 이벤트
# ==========================================

@bot.event
async def on_ready():

    print(f"Logged in as {bot.user}")
    print("Money Battle Royale Bot is ready!")

    await test_database()


# ==========================================
# 실행
# ==========================================

if __name__ == "__main__":

    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()

    token = os.environ.get("DISCORD_TOKEN")

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is missing."
        )


    bot.run(token)
