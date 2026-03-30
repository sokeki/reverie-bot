import os
from dotenv import load_dotenv

load_dotenv()

# ── Credentials ───────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_NAME   = os.getenv("DB_NAME", "discord_points")

# ── Point rules ───────────────────────────────────────────────────────────────
MESSAGES_PER_POINT     = 10   # messages needed to earn 1 point
POINTS_PER_VOICE_BLOCK = 1    # points awarded per voice block
VOICE_BLOCK_MINUTES    = 30   # how many minutes = 1 block  <- change this!
VOICE_TICK_SECONDS     = VOICE_BLOCK_MINUTES * 60

# ── Reverie theme ─────────────────────────────────────────────────────────────
COLOUR_MAIN    = 0x9b8ec4   # muted lavender
COLOUR_LB      = 0x6a5acd   # slate blue
COLOUR_CONFIRM = 0xb8a9d9   # pale lilac
BOT_NAME       = "Reverie"
