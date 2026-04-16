"""
cogs/mudae_cleaner.py
Deletes Mudae roll command messages and their responses after a configurable delay.
Pending deletions are persisted in MongoDB so they survive bot restarts.
"""

import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

MUDAE_ID = 432610292342587392
DEFAULT_DELAY = 60 * 60 * 3  # 3 hours in seconds

# Prefix roll commands to watch for (lowercase, without prefix)
ROLL_COMMANDS = {
    "w",
    "h",
    "m",
    "wg",
    "hg",
    "mg",
    "wa",
    "ha",
    "ma",
    "wai",
    "husbando",
    "marry",
    "mx",
    "mmx",
    "ms",
    "ws",
    "hs",
    "mk",
    "bw",
    "bh",
    "bm",
    "pokemon",
    "pkm",
}


def _parse_delay(value: str) -> int | None:
    value = value.strip().lower()
    try:
        if value.endswith("h"):
            return int(float(value[:-1]) * 3600)
        elif value.endswith("m"):
            return int(float(value[:-1]) * 60)
        elif value.endswith("s"):
            return int(float(value[:-1]))
        else:
            return int(value)
    except ValueError:
        return None


def _format_delay(seconds: int) -> str:
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    elif seconds >= 60:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m {s}s" if s else f"{m}m"
    return f"{seconds}s"


class MudaeCleaner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._scheduled: set[int] = set()

    async def cog_load(self):
        self.process_pending.start()

    async def cog_unload(self):
        self.process_pending.cancel()

    # ── Persistent deletion helpers ───────────────────────────────────────────

    async def _schedule_deletion(self, message: discord.Message, delay: int):
        """Store a pending deletion in MongoDB and schedule the in-memory task."""
        delete_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await self.bot.mudae_deletions_col.insert_one(
            {
                "message_id": message.id,
                "channel_id": message.channel.id,
                "guild_id": message.guild.id,
                "delete_at": delete_at,
            }
        )
        self._scheduled.add(message.id)
        asyncio.create_task(self._delete_after(message.id, message.channel.id, delay))

    async def _delete_after(self, message_id: int, channel_id: int, delay: int):
        """Wait then delete, cleaning up the DB record afterwards."""
        await asyncio.sleep(delay)
        await self._execute_deletion(message_id, channel_id)

    async def _execute_deletion(self, message_id: int, channel_id: int):
        """Delete a message and remove it from the pending deletions collection."""
        try:
            channel = self.bot.get_channel(channel_id)
            if channel:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        finally:
            self._scheduled.discard(message_id)
            await self.bot.mudae_deletions_col.delete_one({"message_id": message_id})

    # ── Startup: process overdue + re-schedule future pending deletions ────────

    @tasks.loop(minutes=5)
    async def process_pending(self):
        now = datetime.now(timezone.utc)

        # Delete anything overdue
        overdue = await self.bot.mudae_deletions_col.find(
            {"delete_at": {"$lte": now}}
        ).to_list(length=1000)
        for doc in overdue:
            if doc["message_id"] not in self._scheduled:
                self._scheduled.add(doc["message_id"])
                asyncio.create_task(
                    self._execute_deletion(doc["message_id"], doc["channel_id"])
                )

        # Re-schedule future deletions (handles restart recovery)
        future = await self.bot.mudae_deletions_col.find(
            {"delete_at": {"$gt": now}}
        ).to_list(length=1000)
        for doc in future:
            if doc["message_id"] in self._scheduled:
                continue  # already has an active task
            delete_at = doc["delete_at"]
            if delete_at.tzinfo is None:
                delete_at = delete_at.replace(tzinfo=timezone.utc)
            delay = max(0, int((delete_at - now).total_seconds()))
            self._scheduled.add(doc["message_id"])
            asyncio.create_task(
                self._delete_after(doc["message_id"], doc["channel_id"], delay)
            )

    @process_pending.before_loop
    async def before_process_pending(self):
        await self.bot.wait_until_ready()

    # ── /mudaecleaner config command ──────────────────────────────────────────

    async def _get_settings(self, guild_id: int) -> dict:
        doc = await self.bot.settings_col.find_one({"guild_id": guild_id}) or {}
        return {
            "enabled": doc.get("mudae_cleaner_enabled", False),
            "delay": doc.get("mudae_cleaner_delay", DEFAULT_DELAY),
        }

    @app_commands.command(
        name="mudaecleaner",
        description="Configure auto-deletion of Mudae roll messages",
    )
    @app_commands.describe(
        enabled="Enable or disable the cleaner",
        delay="Delay before deleting Mudae's response (e.g. 3h, 30m, 60s)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def mudaecleaner(
        self,
        interaction: discord.Interaction,
        enabled: bool = None,
        delay: str = None,
    ):
        settings = await self._get_settings(interaction.guild_id)
        update = {}

        if enabled is not None:
            update["mudae_cleaner_enabled"] = enabled
            settings["enabled"] = enabled

        if delay is not None:
            parsed = _parse_delay(delay)
            if parsed is None or parsed < 0:
                await interaction.response.send_message(
                    "⚠️ Invalid delay. Use formats like `3h`, `30m`, `60s` or a number of seconds.",
                    ephemeral=True,
                )
                return
            update["mudae_cleaner_delay"] = parsed
            settings["delay"] = parsed

        if update:
            await self.bot.settings_col.update_one(
                {"guild_id": interaction.guild_id},
                {"$set": update},
                upsert=True,
            )

        status = "enabled" if settings["enabled"] else "disabled"
        delay_str = _format_delay(settings["delay"])
        await interaction.response.send_message(
            f"Mudae cleaner is **{status}**. Response delay: **{delay_str}**.",
            ephemeral=True,
        )

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        settings = await self._get_settings(message.guild.id)
        if not settings["enabled"]:
            return

        delay = settings["delay"]

        # Mudae roll response - schedule persistent deletion after delay
        if message.author.id == MUDAE_ID:
            for embed in message.embeds:
                desc = (embed.description or "").lower()
                if embed.image and "react with any emoji to claim" in desc:
                    await self._schedule_deletion(message, delay)
                    break
            return

        # User prefix roll command - delete immediately
        if message.author.bot:
            return

        content = message.content.strip()
        if content.startswith("$"):
            cmd = content[1:].split()[0].lower()
            if cmd in ROLL_COMMANDS:
                asyncio.create_task(
                    self._delete_after(message.id, message.channel.id, 0)
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(MudaeCleaner(bot))
