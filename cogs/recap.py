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
        description="[Admin] Manually trigger the weekly recap",
    )
    @app_commands.describe(
        week="Week start date (YYYY-MM-DD, e.g. 2026-04-13). Defaults to current week."
    )
    @app_commands.default_permissions(administrator=True)
    async def sendrecap(self, interaction: discord.Interaction, week: str = None):
        await interaction.response.defer(ephemeral=True)
        sent = await self._post_recap(interaction.guild, week_override=week)
        if sent:
            await interaction.followup.send("✅ Recap posted!", ephemeral=True)
        else:
            await interaction.followup.send(
                "⚠️ No recap channel set or no data for that week.",
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
        now_s = datetime.now(timezone.utc)
        week = (now_s - timedelta(days=(now_s.weekday() + 1) % 7)).strftime("%Y-%m-%d")
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

    async def _get_weekly_deltas(
        self, guild_id: int, week_override: str = None
    ) -> list[dict]:
        """
        Compare current stats against last week's snapshot.
        Returns list of dicts with user_id and weekly gains.
        """
        now_utc = datetime.now(timezone.utc)
        last_week = week_override or (
            now_utc - timedelta(days=(now_utc.weekday() + 1) % 7 + 7)
        ).strftime("%Y-%m-%d")
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

    async def _post_recap(
        self, guild: discord.Guild, week_override: str = None
    ) -> bool:
        settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
        if not settings or not settings.get("recap_channel_id"):
            return False

        channel = guild.get_channel(settings["recap_channel_id"])
        if not channel:
            return False

        deltas = await self._get_weekly_deltas(guild.id, week_override=week_override)
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

        now_utc__ = datetime.now(timezone.utc)
        last_week = week_override or (
            now_utc__ - timedelta(days=(now_utc__.weekday() + 1) % 7 + 7)
        ).strftime("%Y-%m-%d")

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

        # Comp roll stats — use same week key as valorant.py (most recent Sunday)
        now_utc = datetime.now(timezone.utc)
        comp_week = week_override or (
            now_utc - timedelta(days=(now_utc.weekday() + 1) % 7)
        ).strftime("%Y-%m-%d")
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
                        "week": comp_week,
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

        if not any([top_points, top_voice, top_msgs, comp_lines]):
            embed.add_field(
                name="A quiet week",
                value="*no activity recorded this week - come back and earn some points!*",
                inline=False,
            )

        embed.set_footer(text=f"Week of {last_week}  •  Reverie  •  {guild.name}")

        await channel.send(embed=embed)

        # ── Nickname awards ───────────────────────────────────────────────────
        # Priority order for duplicate winners
        NICK_PRIORITY = ["Free Pick", "Duelist", "Initiator", "Sentinel", "Controller"]
        NICK_TITLES = {
            "Free Pick": "freest",
            "Duelist": "happiest player",
            "Initiator": "initiator victim",
            "Sentinel": "chamber role",
            "Controller": "fill main",
        }

        # Find winner per role
        role_winners: dict[str, int] = {}
        for role in NICK_PRIORITY:
            async for doc in (
                self.bot.comp_rolls_col.find(
                    {
                        "guild_id": guild.id,
                        "week": comp_week,
                        "role": role,
                    }
                )
                .sort("count", -1)
                .limit(1)
            ):
                role_winners[role] = doc["user_id"]

        # Resolve duplicates — each user gets only their highest priority role
        assigned: dict[int, str] = {}  # user_id -> role
        for role in NICK_PRIORITY:
            uid = role_winners.get(role)
            if uid and uid not in assigned:
                assigned[uid] = role

        # Step 1: Reset all nicknames that were previously set by Reverie
        prev_settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
        prev_nicks = (prev_settings or {}).get("comp_nick_winners", {})
        # prev_nicks is {str(user_id): old_nickname}
        if isinstance(prev_nicks, list):
            prev_nicks = {str(uid): None for uid in prev_nicks}  # migrate old format
        for uid_str, old_nick in prev_nicks.items():
            m = guild.get_member(int(uid_str))
            if m:
                try:
                    await m.edit(nick=old_nick)  # restores original nickname
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Step 2: Apply new nicknames
        new_nicks = {}  # {str(user_id): old_nickname}
        for uid, role in assigned.items():
            m = guild.get_member(uid)
            if not m:
                continue
            old_nick = m.nick  # store current nickname before changing
            title = NICK_TITLES[role]
            base = m.nick or m.display_name
            new_nick = f"{base} ({title})"
            if len(new_nick) > 32:
                max_base = 32 - len(f" ({title})") - 1
                new_nick = f"{base[:max_base]} ({title})"
            try:
                await m.edit(nick=new_nick)
                new_nicks[str(uid)] = old_nick
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Store winners + their old nicknames for reset next week
        await self.bot.settings_col.update_one(
            {"guild_id": guild.id},
            {"$set": {"comp_nick_winners": new_nicks}},
            upsert=True,
        )

        # Take a fresh snapshot for next week
        await self._take_snapshot(guild.id)

        # Clear this week's comp rolls - they reset weekly
        await self.bot.comp_rolls_col.delete_many(
            {
                "guild_id": guild.id,
                "week": comp_week,
            }
        )

        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(Recap(bot))
