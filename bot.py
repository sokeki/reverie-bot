import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient

import os
from config import (
    BOT_TOKEN,
    MONGO_URI,
    DB_NAME,
    MESSAGES_PER_POINT,
    BOT_NAME,
)
from utils.streaks import record_activity as _record_streak

GUILD_ID = int(os.getenv("GUILD_ID", "0"))


async def get_live_settings(bot) -> dict:
    """Fetch current settings from MongoDB, falling back to config.py defaults."""
    doc = await bot.settings_col.find_one({"guild_id": GUILD_ID})
    return {
        "messages_per_point": (
            doc.get("messages_per_point", MESSAGES_PER_POINT)
            if doc
            else MESSAGES_PER_POINT
        ),
        "voice_block_minutes": doc.get("voice_block_minutes", 30) if doc else 30,
        "points_per_voice_block": doc.get("points_per_voice_block", 1) if doc else 1,
    }


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
    bot.settings_col = db["guild_settings"]
    bot.voice_sessions_col = db["voice_sessions"]
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

    # Pre-populate all existing server members and always refresh username/avatar
    added = 0
    updated = 0
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            result = await bot.users_col.update_one(
                {"user_id": member.id, "guild_id": guild.id},
                {
                    "$setOnInsert": {
                        "user_id": member.id,
                        "guild_id": guild.id,
                        "points": 0,
                        "voice_minutes": 0,
                        "messages_sent": 0,
                        "streak": 0,
                        "streak_best": 0,
                        "streak_last_date": None,
                    },
                    "$set": {
                        "username": member.display_name,
                        "avatar_url": str(member.display_avatar.url),
                    },
                },
                upsert=True,
            )
            if result.upserted_id:
                added += 1
            else:
                updated += 1
    print(f"✨  Pre-populated {added} new and refreshed {updated} existing member(s).")

    # Sync role colours for all shop role items on startup
    colour_synced = 0
    for guild in bot.guilds:
        shop_items = await bot.items_col.find(
            {"guild_id": guild.id, "type": "role"}
        ).to_list(length=100)
        for item in shop_items:
            role = guild.get_role(item.get("role_id"))
            if role:
                colour = f"{role.colour.value:06X}" if role.colour.value else None
                await bot.items_col.update_one(
                    {"_id": item["_id"]},
                    {"$set": {"role_colour": colour}},
                )
                colour_synced += 1
    print(f"🎨  Synced role colours for {colour_synced} shop item(s).")


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
            },
            "$set": {
                "username": message.author.display_name,
                "avatar_url": str(message.author.display_avatar.url),
            },
        },
        upsert=True,
    )
    # Increment message count and fetch updated doc
    result = await bot.users_col.find_one_and_update(
        {"user_id": message.author.id, "guild_id": message.guild.id},
        {"$inc": {"messages_sent": 1}},
        return_document=True,
    )
    # Award 1 point per live messages_per_point setting
    settings = await get_live_settings(bot)
    if result and result.get("messages_sent", 0) % settings["messages_per_point"] == 0:
        await bot.users_col.update_one(
            {"user_id": message.author.id, "guild_id": message.guild.id},
            {"$inc": {"points": 1}},
        )

    # Record streak activity
    await _record_streak(bot.users_col, message.author.id, message.guild.id)

    await bot.process_commands(message)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Update username and avatar whenever a member changes their profile."""
    if after.bot:
        return
    if (
        before.display_name == after.display_name
        and before.display_avatar == after.display_avatar
    ):
        return  # nothing relevant changed
    await bot.users_col.update_one(
        {"user_id": after.id, "guild_id": after.guild.id},
        {
            "$set": {
                "username": after.display_name,
                "avatar_url": str(after.display_avatar.url),
            }
        },
    )


@bot.event
async def on_user_update(before: discord.User, after: discord.User):
    """Update username and avatar when a user changes their global Discord profile."""
    if after.bot:
        return
    if (
        before.display_name == after.display_name
        and before.display_avatar == after.display_avatar
    ):
        return
    await bot.users_col.update_many(
        {"user_id": after.id},
        {
            "$set": {
                "username": after.display_name,
                "avatar_url": str(after.display_avatar.url),
            }
        },
    )


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
            },
            "$set": {
                "username": member.display_name,
                "avatar_url": str(member.display_avatar.url),
            },
        },
        upsert=True,
    )


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    """Update role_colour in shop items when a role's colour changes in Discord."""
    if before.colour == after.colour:
        return
    colour = f"{after.colour.value:06X}" if after.colour.value else None
    await bot.items_col.update_one(
        {"guild_id": after.guild.id, "role_id": after.id},
        {"$set": {"role_colour": colour}},
    )


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in .env")
    bot.run(BOT_TOKEN)
