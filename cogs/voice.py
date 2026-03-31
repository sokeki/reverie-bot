import discord
from discord.ext import commands, tasks
from datetime import datetime

from config import VOICE_TICK_SECONDS


def _minutes_since(dt: datetime) -> int:
    return int((datetime.utcnow() - dt).total_seconds() // 60)


class Voice(commands.Cog):
    """Tracks voice channel time and awards points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_join_times: dict[int, datetime] = {}
        self.voice_point_ticker.start()

    def cog_unload(self):
        self.voice_point_ticker.cancel()

    async def _get_voice_settings(self) -> tuple[int, int]:
        """Return (voice_block_minutes, points_per_voice_block) from live DB settings."""
        doc = await self.bot.settings_col.find_one({})
        if doc:
            return doc.get("voice_block_minutes", 30), doc.get(
                "points_per_voice_block", 1
            )
        return 30, 1

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

        elif left and member.id in self.voice_join_times:
            block_mins, pts_per_block = await self._get_voice_settings()
            minutes = _minutes_since(self.voice_join_times.pop(member.id))
            pts = (minutes // block_mins) * pts_per_block
            update = {"$inc": {"voice_minutes": minutes}}
            if pts:
                update["$inc"]["points"] = pts
            await self.bot.users_col.update_one(
                {"user_id": member.id, "guild_id": member.guild.id},
                update,
                upsert=True,
            )

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
                        continue
                    minutes = _minutes_since(self.voice_join_times[member.id])
                    if minutes >= block_mins:
                        pts = (minutes // block_mins) * pts_per_block
                        await self.bot.users_col.update_one(
                            {"user_id": member.id, "guild_id": guild.id},
                            {"$inc": {"points": pts, "voice_minutes": minutes}},
                            upsert=True,
                        )
                        self.voice_join_times[member.id] = now

    @voice_point_ticker.before_loop
    async def before_ticker(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
