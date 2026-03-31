import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient

from config import (
    BOT_TOKEN,
    MONGO_URI,
    DB_NAME,
    MESSAGES_PER_POINT,
    BOT_NAME,
)

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="r!", intents=intents)

# Cogs to load on startup
COGS = [
    "cogs.points",
    "cogs.leaderboard",
    "cogs.admin",
    "cogs.voice",
    "cogs.shop",
    "cogs.valorant",
]


# ── Events ────────────────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    # Connect to MongoDB and attach collections to the bot instance
    # so all cogs can access them via self.bot.users_col
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    bot.users_col = db["users"]
    bot.items_col = db["shop_items"]
    bot.inv_col = db["inventories"]
    await bot.users_col.create_index([("guild_id", 1), ("points", -1)])
    await bot.items_col.create_index([("guild_id", 1), ("name", 1)])

    # Load all cogs
    for cog in COGS:
        await bot.load_extension(cog)

    # Sync slash commands globally
    synced = await bot.tree.sync()
    print(f"🌙  {BOT_NAME} online as {bot.user} — {len(synced)} slash commands synced.")

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="the space between sleep and waking ✨",
        ),
    )

    # Pre-populate all existing server members
    added = 0
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            existing = await bot.users_col.find_one(
                {"user_id": member.id, "guild_id": guild.id}
            )
            if existing is None:
                await bot.users_col.insert_one(
                    {
                        "user_id": member.id,
                        "guild_id": guild.id,
                        "points": 0,
                        "voice_minutes": 0,
                        "messages_sent": 0,
                    }
                )
                added += 1
    print(f"✨  Pre-populated {added} new member(s) into the DB.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # Ensure doc exists with all fields before incrementing
    await bot.users_col.update_one(
        {"user_id": message.author.id, "guild_id": message.guild.id},
        {
            "$setOnInsert": {
                "user_id": message.author.id,
                "guild_id": message.guild.id,
                "points": 0,
                "voice_minutes": 0,
                "messages_sent": 0,
            }
        },
        upsert=True,
    )
    # Increment message count and fetch updated doc
    result = await bot.users_col.find_one_and_update(
        {"user_id": message.author.id, "guild_id": message.guild.id},
        {"$inc": {"messages_sent": 1}},
        return_document=True,
    )
    # Award 1 point every MESSAGES_PER_POINT messages
    if result and result.get("messages_sent", 0) % MESSAGES_PER_POINT == 0:
        await bot.users_col.update_one(
            {"user_id": message.author.id, "guild_id": message.guild.id},
            {"$inc": {"points": 1}},
        )

    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    """Add brand new members to the DB as soon as they join."""
    if member.bot:
        return
    await bot.users_col.update_one(
        {"user_id": member.id, "guild_id": member.guild.id},
        {
            "$setOnInsert": {
                "user_id": member.id,
                "guild_id": member.guild.id,
                "points": 0,
                "voice_minutes": 0,
                "messages_sent": 0,
            }
        },
        upsert=True,
    )


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in .env")
    bot.run(BOT_TOKEN)
