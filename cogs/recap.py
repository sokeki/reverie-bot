import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta

from config import COLOUR_LB
from utils.ranks import get_rank


GUILD_ID = 0  # set from env on startup


def _fmt_voice(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _week_start() -> str:
    """ISO date string for the most recent Sunday (UTC)."""
    now = datetime.now(timezone.utc)
    days = now.weekday() + 1  # Monday=0, so Sunday is 6 days back from Monday
    sunday = now - timedelta(days=days % 7)
    return sunday.strftime("%Y-%m-%d")


class Recap(commands.Cog):
    """Posts a weekly server recap every Sunday at midnight UTC."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_recap_task.start()

    def cog_unload(self):
        self.weekly_recap_task.cancel()

    # ── /setrecapchannel ──────────────────────────────────────────────────────

    @app_commands.command(
        name="setrecapchannel",
        description="[Admin] Set the channel where the weekly recap is posted",
    )
    @app_commands.describe(channel="Channel to post the weekly recap in")
    @app_commands.default_permissions(administrator=True)
    async def setrecapchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"recap_channel_id": channel.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Weekly recap will be posted in {channel.mention} every Sunday at midnight UTC.",
            ephemeral=True,
        )

    # ── /sendrecap (admin, manual trigger) ───────────────────────────────────

    @app_commands.command(
        name="sendrecap",
        description="[Admin] Manually trigger this week's recap",
    )
    @app_commands.default_permissions(administrator=True)
    async def sendrecap(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sent = await self._post_recap(interaction.guild)
        if sent:
            await interaction.followup.send("✅ Recap posted!", ephemeral=True)
        else:
            await interaction.followup.send(
                "⚠️ No recap channel set. Run `/setrecapchannel` first.",
                ephemeral=True,
            )

    # ── Background task ───────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def weekly_recap_task(self):
        now = datetime.now(timezone.utc)
        # Fire on Monday (weekday 0) at midnight UTC
        # Use a 5 minute window to be robust against Heroku clock drift
        if now.weekday() != 0:
            return
        if now.hour != 0 or now.minute >= 5:
            return

        # Use last_recap_date to ensure we only post once per week
        today = now.strftime("%Y-%m-%d")
        for guild in self.bot.guilds:
            settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
            if settings and settings.get("last_recap_date") == today:
                continue
            await self._post_recap(guild)
            await self.bot.settings_col.update_one(
                {"guild_id": guild.id},
                {"$set": {"last_recap_date": today}},
                upsert=True,
            )

    @weekly_recap_task.before_loop
    async def before_recap(self):
        await self.bot.wait_until_ready()

    # ── Snapshot helpers ──────────────────────────────────────────────────────

    async def _take_snapshot(self, guild_id: int):
        """Save current stats for all members so we can diff next week."""
        week = _week_start()
        docs = await self.bot.users_col.find(
            {"guild_id": guild_id},
            {"user_id": 1, "points": 1, "voice_minutes": 1, "messages_sent": 1},
        ).to_list(length=5000)

        snapshots = [
            {
                "guild_id": guild_id,
                "week": week,
                "user_id": d["user_id"],
                "points": d.get("points", 0),
                "voice_minutes": d.get("voice_minutes", 0),
                "messages_sent": d.get("messages_sent", 0),
            }
            for d in docs
        ]
        if snapshots:
            await self.bot.weekly_snapshots_col.delete_many(
                {"guild_id": guild_id, "week": week}
            )
            await self.bot.weekly_snapshots_col.insert_many(snapshots)

    async def _get_weekly_deltas(self, guild_id: int) -> list[dict]:
        """
        Compare current stats against last week's snapshot.
        Returns list of dicts with user_id and weekly gains.
        """
        last_week = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )
        snapshots = await self.bot.weekly_snapshots_col.find(
            {"guild_id": guild_id, "week": last_week}
        ).to_list(length=5000)
        snap_map = {s["user_id"]: s for s in snapshots}

        current_docs = await self.bot.users_col.find(
            {"guild_id": guild_id},
            {"user_id": 1, "points": 1, "voice_minutes": 1, "messages_sent": 1},
        ).to_list(length=5000)

        deltas = []
        for doc in current_docs:
            uid = doc["user_id"]
            snap = snap_map.get(uid, {})
            deltas.append(
                {
                    "user_id": uid,
                    "points_gained": doc.get("points", 0) - snap.get("points", 0),
                    "voice_gained": doc.get("voice_minutes", 0)
                    - snap.get("voice_minutes", 0),
                    "msgs_gained": doc.get("messages_sent", 0)
                    - snap.get("messages_sent", 0),
                }
            )

        return deltas

    # ── Recap builder ─────────────────────────────────────────────────────────

    async def _post_recap(self, guild: discord.Guild) -> bool:
        settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
        if not settings or not settings.get("recap_channel_id"):
            return False

        channel = guild.get_channel(settings["recap_channel_id"])
        if not channel:
            return False

        deltas = await self._get_weekly_deltas(guild.id)
        if not deltas:
            return False

        def _name(uid: int) -> str:
            m = guild.get_member(uid)
            return m.display_name if m else f"Dreamer {uid}"

        medals = ["🥇", "🥈", "🥉"]

        # Top 3 by points gained
        top_points = sorted(
            [d for d in deltas if d["points_gained"] > 0],
            key=lambda d: d["points_gained"],
            reverse=True,
        )[:3]

        # Top 3 by voice time gained
        top_voice = sorted(
            [d for d in deltas if d["voice_gained"] > 0],
            key=lambda d: d["voice_gained"],
            reverse=True,
        )[:3]

        # Top 3 by messages sent
        top_msgs = sorted(
            [d for d in deltas if d["msgs_gained"] > 0],
            key=lambda d: d["msgs_gained"],
            reverse=True,
        )[:3]

        # New rank achievements this week - compare rank from snapshot vs now
        last_week = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )
        snapshots = await self.bot.weekly_snapshots_col.find(
            {"guild_id": guild.id, "week": last_week}
        ).to_list(length=5000)
        snap_map = {s["user_id"]: s for s in snapshots}
        current_docs = await self.bot.users_col.find(
            {"guild_id": guild.id},
            {"user_id": 1, "voice_minutes": 1, "messages_sent": 1},
        ).to_list(length=5000)

        rank_ups = []
        for doc in current_docs:
            uid = doc["user_id"]
            snap = snap_map.get(uid, {})
            old_score = snap.get("voice_minutes", 0) + snap.get("messages_sent", 0)
            new_score = doc.get("voice_minutes", 0) + doc.get("messages_sent", 0)
            old_rank = get_rank(old_score)
            new_rank = get_rank(new_score)
            if new_rank["index"] > old_rank["index"]:
                rank_ups.append(
                    {
                        "user_id": uid,
                        "old_rank": old_rank,
                        "new_rank": new_rank,
                    }
                )

        # Build embed
        embed = discord.Embed(
            title="🌙 Weekly Recap",
            description=f"*here's how {guild.name} dreamed this week...*",
            color=COLOUR_LB,
        )

        # Points field
        if top_points:
            lines = [
                f"{medals[i]} **{_name(d['user_id'])}** - +{d['points_gained']:,} pts"
                for i, d in enumerate(top_points)
            ]
            embed.add_field(
                name="✨ Most Points Earned", value="\n".join(lines), inline=False
            )

        # Voice field
        if top_voice:
            lines = [
                f"{medals[i]} **{_name(d['user_id'])}** - {_fmt_voice(d['voice_gained'])}"
                for i, d in enumerate(top_voice)
            ]
            embed.add_field(
                name="🎙️ Most Time in Voice", value="\n".join(lines), inline=False
            )

        # Messages field
        if top_msgs:
            lines = [
                f"{medals[i]} **{_name(d['user_id'])}** - {d['msgs_gained']:,} messages"
                for i, d in enumerate(top_msgs)
            ]
            embed.add_field(
                name="💬 Most Messages Sent", value="\n".join(lines), inline=False
            )

        # Rank ups field
        if rank_ups:
            lines = [
                f"**{_name(r['user_id'])}** - {r['old_rank']['symbol']} {r['old_rank']['name']} - {r['new_rank']['symbol']} {r['new_rank']['name']}"
                for r in rank_ups[:10]
            ]
            embed.add_field(
                name="🏅 Rank Achievements", value="\n".join(lines), inline=False
            )

        # Comp roll stats
        ROLE_LABELS = {
            "Duelist": "🎯 Happiest five stack player",
            "Initiator": "🔍 Initiator victim",
            "Controller": "💨 Smokes fill main",
            "Sentinel": "🔒 Chamber role",
            "Free Pick": "🌀 Freest",
        }
        comp_lines = []
        for role, label in ROLE_LABELS.items():
            # Find who got this role the most this week
            top = None
            top_count = 0
            async for doc in (
                self.bot.comp_rolls_col.find(
                    {
                        "guild_id": guild.id,
                        "week": last_week,
                        "role": role,
                    }
                )
                .sort("count", -1)
                .limit(1)
            ):
                top = doc
                top_count = doc["count"]

            if top:
                m = guild.get_member(top["user_id"])
                name = m.display_name if m else f"Dreamer {top['user_id']}"
                comp_lines.append(f"{label}: **{name}** ({top_count}x)")

        if comp_lines:
            embed.add_field(
                name="🎲 Comp Roll Awards",
                value="\n".join(comp_lines),
                inline=False,
            )

        if not any([top_points, top_voice, top_msgs, rank_ups, comp_lines]):
            embed.add_field(
                name="A quiet week",
                value="*no activity recorded this week - come back and earn some points!*",
                inline=False,
            )

        embed.set_footer(text=f"Week of {last_week}  -  Reverie  -  {guild.name}")

        await channel.send(embed=embed)

        # Take a fresh snapshot for next week
        await self._take_snapshot(guild.id)

        # Clear this week's comp rolls - they reset weekly
        await self.bot.comp_rolls_col.delete_many(
            {
                "guild_id": guild.id,
                "week": last_week,
            }
        )

        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(Recap(bot))
