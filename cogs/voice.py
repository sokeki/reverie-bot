import discord
from discord.ext import commands, tasks
from datetime import datetime

from config import POINTS_PER_VOICE_BLOCK, VOICE_BLOCK_MINUTES, VOICE_TICK_SECONDS


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
            minutes = _minutes_since(self.voice_join_times.pop(member.id))
            pts = (minutes // VOICE_BLOCK_MINUTES) * POINTS_PER_VOICE_BLOCK
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
                    if minutes >= VOICE_BLOCK_MINUTES:
                        pts = (minutes // VOICE_BLOCK_MINUTES) * POINTS_PER_VOICE_BLOCK
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
