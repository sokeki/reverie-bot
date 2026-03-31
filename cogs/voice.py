import os
import discord
from utils.streaks import record_activity as _record_streak
from discord.ext import commands, tasks
from datetime import datetime, timezone

from config import VOICE_TICK_SECONDS

VOICE_MINUTES_SYNC_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _minutes_between(start: datetime, end: datetime) -> int:
    # Ensure both are timezone-aware
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return int((end - start).total_seconds() // 60)


def _minutes_since(dt: datetime) -> int:
    return _minutes_between(dt, _utcnow())


class Voice(commands.Cog):
    """Tracks voice channel time and awards points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_join_times: dict[int, datetime] = {}
        self.last_sync: dict[int, datetime] = {}
        self.voice_point_ticker.start()
        self.voice_minutes_sync.start()

    def cog_unload(self):
        self.voice_point_ticker.cancel()
        self.voice_minutes_sync.cancel()

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _get_voice_settings(self) -> tuple[int, int]:
        guild_id = int(os.getenv("GUILD_ID", "0"))
        doc = await self.bot.settings_col.find_one({"guild_id": guild_id})
        if doc:
            return doc.get("voice_block_minutes", 30), doc.get(
                "points_per_voice_block", 1
            )
        return 30, 1

    async def _save_session(
        self, user_id: int, guild_id: int, join_time: datetime, sync_time: datetime
    ):
        """Persist voice session times to MongoDB so restarts don't lose them."""
        await self.bot.voice_sessions_col.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {
                "$set": {
                    "join_time": join_time,
                    "sync_time": sync_time,
                }
            },
            upsert=True,
        )

    async def _clear_session(self, user_id: int, guild_id: int):
        """Remove a voice session from MongoDB when the member leaves."""
        await self.bot.voice_sessions_col.delete_one(
            {"user_id": user_id, "guild_id": guild_id}
        )

    async def _restore_sessions(self):
        """On startup, restore join times for anyone already in a VC.
        Also recover sessions for members who were in VC during a restart."""
        now = _utcnow()

        # First restore any persisted sessions from before the restart
        async for session in self.bot.voice_sessions_col.find({}):
            user_id = session["user_id"]
            join_time = session["join_time"]
            sync_time = session.get("sync_time", join_time)

            # Make timezone-aware if stored as naive
            if join_time.tzinfo is None:
                join_time = join_time.replace(tzinfo=timezone.utc)
            if sync_time.tzinfo is None:
                sync_time = sync_time.replace(tzinfo=timezone.utc)

            self.voice_join_times[user_id] = join_time
            self.last_sync[user_id] = sync_time

        # Then seed any members currently in VC who don't have a persisted session
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    if member.id not in self.voice_join_times:
                        self.voice_join_times[member.id] = now
                        self.last_sync[member.id] = now
                        await self._save_session(member.id, guild.id, now, now)

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
            now = _utcnow()
            self.voice_join_times[member.id] = now
            self.last_sync[member.id] = now
            await self._save_session(member.id, member.guild.id, now, now)
            await _record_streak(self.bot.users_col, member.id, member.guild.id)

        elif left and member.id in self.voice_join_times:
            block_mins, pts_per_block = await self._get_voice_settings()
            now = _utcnow()
            join_time = self.voice_join_times.pop(member.id)
            sync_time = self.last_sync.pop(member.id, join_time)
            total_mins = _minutes_between(join_time, now)
            unsynced_mins = _minutes_between(sync_time, now)

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
            await self._clear_session(member.id, member.guild.id)

    # ── Points ticker (every VOICE_BLOCK_MINUTES) ─────────────────────────────

    @tasks.loop(seconds=VOICE_TICK_SECONDS)
    async def voice_point_ticker(self):
        block_mins, pts_per_block = await self._get_voice_settings()
        now = _utcnow()
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    if member.id not in self.voice_join_times:
                        self.voice_join_times[member.id] = now
                        self.last_sync[member.id] = now
                        await self._save_session(member.id, guild.id, now, now)
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
                        await self._save_session(
                            member.id, guild.id, now, self.last_sync.get(member.id, now)
                        )

    # ── Minutes sync ticker (every 60 seconds) ────────────────────────────────

    @tasks.loop(seconds=VOICE_MINUTES_SYNC_SECONDS)
    async def voice_minutes_sync(self):
        now = _utcnow()
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
                    mins_since_sync = _minutes_between(last, now)
                    if mins_since_sync >= 1:
                        await self.bot.users_col.update_one(
                            {"user_id": member.id, "guild_id": guild.id},
                            {"$inc": {"voice_minutes": mins_since_sync}},
                            upsert=True,
                        )
                        self.last_sync[member.id] = now
                        await self._save_session(
                            member.id,
                            guild.id,
                            self.voice_join_times[member.id],
                            now,
                        )

    @voice_point_ticker.before_loop
    async def before_point_ticker(self):
        await self.bot.wait_until_ready()
        await self._restore_sessions()

    @voice_minutes_sync.before_loop
    async def before_minutes_sync(self):
        # Wait for voice_point_ticker to finish restoring sessions first
        await self.bot.wait_until_ready()
        while not self.voice_point_ticker.is_running():
            import asyncio

            await asyncio.sleep(0.5)


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
