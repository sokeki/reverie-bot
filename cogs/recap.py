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

    # ── /setcompwinnerrole ───────────────────────────────────────────────────

    @app_commands.command(
        name="setcompwinnerrole",
        description="[Admin] Set the shared role given to all comp roll winners each week",
    )
    @app_commands.describe(role="Role to assign to winners")
    @app_commands.default_permissions(administrator=True)
    async def setcompwinnerrole(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"comp_winner_role_id": role.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Comp roll winner role set to {role.mention}.",
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
        # Upsert each snapshot so history accumulates across weeks
        for snap in snapshots:
            await self.bot.weekly_snapshots_col.update_one(
                {
                    "guild_id": snap["guild_id"],
                    "week": snap["week"],
                    "user_id": snap["user_id"],
                },
                {"$set": snap},
                upsert=True,
            )

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

        # ── Nickname & role awards ────────────────────────────────────────────
        NICK_PRIORITY = ["Free Pick", "Duelist", "Initiator", "Sentinel", "Controller"]
        NICK_TITLES = {
            "Free Pick": "freest",
            "Duelist": "happiest five stack player",
            "Initiator": "initiator victim",
            "Sentinel": "chamber role",
            "Controller": "smokes fill main",
        }

        # Find winner per role
        role_winners: dict[str, int] = {}
        for comp_role in NICK_PRIORITY:
            async for doc in (
                self.bot.comp_rolls_col.find(
                    {
                        "guild_id": guild.id,
                        "week": comp_week,
                        "role": comp_role,
                    }
                )
                .sort("count", -1)
                .limit(1)
            ):
                role_winners[comp_role] = doc["user_id"]

        # All role winners — duplicates allowed, same person can get multiple titles
        # assigned is now role -> user_id
        assigned: dict[str, int] = {r: uid for r, uid in role_winners.items()}
        # For nicknames, track unique winners (crown only needs applying once)
        nick_winners: dict[int, str] = {}  # user_id -> first comp_role (for nick)
        for comp_role in NICK_PRIORITY:
            uid = role_winners.get(comp_role)
            if uid and uid not in nick_winners:
                nick_winners[uid] = comp_role

        settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
        winner_role_id = (settings or {}).get("comp_winner_role_id")
        winner_role = guild.get_role(winner_role_id) if winner_role_id else None

        # Step 1: Reset previous winners
        prev_data = (settings or {}).get("comp_nick_winners", {})
        if isinstance(prev_data, list):
            prev_data = {str(uid): {} for uid in prev_data}
        for uid_str, saved in prev_data.items():
            m = guild.get_member(int(uid_str))
            if not m:
                continue
            try:
                await m.edit(
                    nick=saved.get("nick") if isinstance(saved, dict) else saved
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            if winner_role and winner_role in m.roles:
                try:
                    await m.remove_roles(winner_role)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            title_role_ids = (
                saved.get("title_role_ids", []) if isinstance(saved, dict) else []
            )
            # Also handle old single title_role_id format
            if (
                not title_role_ids
                and isinstance(saved, dict)
                and saved.get("title_role_id")
            ):
                title_role_ids = [saved["title_role_id"]]
            for trid in title_role_ids:
                title_role = guild.get_role(trid)
                if title_role and title_role in m.roles:
                    try:
                        await m.remove_roles(title_role)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        # Step 2: Apply crowns and shared role to unique winners
        new_data = {}
        saved_old_nicks: dict[int, str | None] = {}
        for uid in nick_winners:
            m = guild.get_member(uid)
            if not m:
                continue
            old_nick = m.nick
            saved_old_nicks[uid] = old_nick
            base = m.nick or m.display_name
            new_nick = f"👑 {base}"
            if len(new_nick) > 32:
                new_nick = f"👑 {base[:29]}"
            try:
                await m.edit(nick=new_nick)
            except (discord.Forbidden, discord.HTTPException):
                pass
            if winner_role:
                try:
                    await m.add_roles(winner_role)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            new_data[str(uid)] = {"nick": old_nick, "title_role_ids": []}

        # Step 3: Apply title roles (allow duplicates — same person can get multiple)
        for comp_role, uid in assigned.items():
            m = guild.get_member(uid)
            if not m:
                continue
            title_name = NICK_TITLES[comp_role]
            title_role = discord.utils.get(guild.roles, name=title_name)
            if not title_role:
                try:
                    title_role = await guild.create_role(name=title_name)
                except (discord.Forbidden, discord.HTTPException):
                    title_role = None
            if title_role:
                try:
                    await m.add_roles(title_role)
                except (discord.Forbidden, discord.HTTPException):
                    pass
                uid_str = str(uid)
                if uid_str not in new_data:
                    new_data[uid_str] = {
                        "nick": saved_old_nicks.get(uid),
                        "title_role_ids": [],
                    }
                new_data[uid_str]["title_role_ids"].append(title_role.id)

        await self.bot.settings_col.update_one(
            {"guild_id": guild.id},
            {"$set": {"comp_nick_winners": new_data}},
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
