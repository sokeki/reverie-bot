import os
import discord
from discord.ext import commands, tasks
from datetime import datetime

from config import VOICE_TICK_SECONDS

# How often voice_minutes is written to DB for live display (independent of points)
VOICE_MINUTES_SYNC_SECONDS = 60


def _minutes_since(dt: datetime) -> int:
    return int((datetime.utcnow() - dt).total_seconds() // 60)


class Voice(commands.Cog):
    """Tracks voice channel time and awards points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_join_times: dict[int, datetime] = {}
        # last time voice_minutes was synced to DB per user (for the display ticker)
        self.last_sync: dict[int, datetime] = {}
        self.voice_point_ticker.start()
        self.voice_minutes_sync.start()

    def cog_unload(self):
        self.voice_point_ticker.cancel()
        self.voice_minutes_sync.cancel()

    async def _get_voice_settings(self) -> tuple[int, int]:
        """Return (voice_block_minutes, points_per_voice_block) from live DB settings."""
        guild_id = int(os.getenv("GUILD_ID", "0"))
        doc = await self.bot.settings_col.find_one({"guild_id": guild_id})
        if doc:
            return doc.get("voice_block_minutes", 30), doc.get(
                "points_per_voice_block", 1
            )
        return 30, 1

    # ── Voice state listener ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        joined = after.channel is not None and before.channel is None
        left = after.channel is None and before.channel is not None

        if joined:
            self.voice_join_times[member.id] = datetime.utcnow()
            self.last_sync[member.id] = datetime.utcnow()

        elif left and member.id in self.voice_join_times:
            block_mins, pts_per_block = await self._get_voice_settings()
            join_time = self.voice_join_times.pop(member.id)
            sync_time = self.last_sync.pop(member.id, join_time)
            total_mins = _minutes_since(join_time)

            # Minutes since last sync (to avoid double-counting what was already written)
            unsynced_mins = _minutes_since(sync_time)

            pts = (total_mins // block_mins) * pts_per_block
            update = {"$inc": {"voice_minutes": unsynced_mins}}
            if pts:
                update["$inc"]["points"] = pts
            if unsynced_mins > 0 or pts:
                await self.bot.users_col.update_one(
                    {"user_id": member.id, "guild_id": member.guild.id},
                    update,
                    upsert=True,
                )

    # ── Points ticker (every VOICE_BLOCK_MINUTES) ─────────────────────────────

    @tasks.loop(seconds=VOICE_TICK_SECONDS)
    async def voice_point_ticker(self):
        block_mins, pts_per_block = await self._get_voice_settings()
        now = datetime.utcnow()
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    if member.id not in self.voice_join_times:
                        self.voice_join_times[member.id] = now
                        self.last_sync[member.id] = now
                        continue
                    minutes = _minutes_since(self.voice_join_times[member.id])
                    if minutes >= block_mins:
                        pts = (minutes // block_mins) * pts_per_block
                        await self.bot.users_col.update_one(
                            {"user_id": member.id, "guild_id": guild.id},
                            {"$inc": {"points": pts}},
                            upsert=True,
                        )
                        self.voice_join_times[member.id] = now

    # ── Minutes sync ticker (every 60 seconds) ────────────────────────────────

    @tasks.loop(seconds=VOICE_MINUTES_SYNC_SECONDS)
    async def voice_minutes_sync(self):
        """Write accumulated voice_minutes to DB every minute for live dashboard display."""
        now = datetime.utcnow()
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    if member.id not in self.voice_join_times:
                        continue
                    last = self.last_sync.get(
                        member.id, self.voice_join_times[member.id]
                    )
                    mins_since_sync = _minutes_since(last)
                    if mins_since_sync >= 1:
                        await self.bot.users_col.update_one(
                            {"user_id": member.id, "guild_id": guild.id},
                            {"$inc": {"voice_minutes": mins_since_sync}},
                            upsert=True,
                        )
                        self.last_sync[member.id] = now

    @voice_point_ticker.before_loop
    async def before_point_ticker(self):
        await self.bot.wait_until_ready()

    @voice_minutes_sync.before_loop
    async def before_minutes_sync(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
