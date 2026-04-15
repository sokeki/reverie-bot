"""
cogs/mudae_cleaner.py
Deletes Mudae roll command messages and their responses after a configurable delay.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

MUDAE_ID = 432610292342587392

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

DEFAULT_DELAY = 60 * 60 * 3  # 3 hours in seconds


def _parse_delay(value: str) -> int | None:
    """Parse a delay string like '3h', '30m', '60s', or plain seconds."""
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


async def _try_delete(msg: discord.Message, delay: int = 0):
    """Delete a message after a delay, ignoring errors."""
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class MudaeCleaner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_settings(self, guild_id: int) -> dict:
        doc = await self.bot.settings_col.find_one({"guild_id": guild_id}) or {}
        return {
            "enabled": doc.get("mudae_cleaner_enabled", False),
            "delay": doc.get("mudae_cleaner_delay", DEFAULT_DELAY),
        }

    # ── /setmuddaecleaner ─────────────────────────────────────────────────────

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

        # Format delay for display
        d = settings["delay"]
        if d >= 3600:
            delay_str = (
                f"{d // 3600}h {(d % 3600) // 60}m" if d % 3600 else f"{d // 3600}h"
            )
        elif d >= 60:
            delay_str = f"{d // 60}m {d % 60}s" if d % 60 else f"{d // 60}m"
        else:
            delay_str = f"{d}s"

        status = "enabled" if settings["enabled"] else "disabled"
        await interaction.response.send_message(
            f"Mudae cleaner is **{status}**. Response delay: **{delay_str}**.",
            ephemeral=True,
        )

    # ── Listen for roll command messages ─────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        settings = await self._get_settings(message.guild.id)
        if not settings["enabled"]:
            return

        delay = settings["delay"]

        # Check if this is a Mudae response to a roll (embed with image from Mudae)
        if message.author.id == MUDAE_ID:
            # Only delete roll result embeds — these have an image attached
            if message.embeds and any(e.image or e.thumbnail for e in message.embeds):
                asyncio.create_task(_try_delete(message, delay))
            return

        # Check if this is a user roll command (prefix)
        if message.author.bot:
            return

        content = message.content.strip()
        # Match $command or $command<args>
        for prefix in ("$", message.guild.me.mention):
            if content.startswith(prefix):
                cmd = content[len(prefix) :].split()[0].lower().lstrip("$")
                if cmd in ROLL_COMMANDS:
                    asyncio.create_task(_try_delete(message, 0))
                    return

        # Slash commands from users — we can't see these directly, but we can
        # detect the interaction response by watching for Mudae embeds above


async def setup(bot: commands.Bot):
    await bot.add_cog(MudaeCleaner(bot))
