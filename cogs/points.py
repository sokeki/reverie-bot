import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_MAIN
from utils.db import get_user
from utils.ranks import get_rank

# Invisible spacer for Discord embed column alignment
_BLANK = "\u200b"


class Points(commands.Cog):
    """Handles the /points slash command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="points",
        description="Check your dream points, or peek at another member's",
    )
    @app_commands.describe(member="The member to check (leave empty for yourself)")
    async def points(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        target = member or interaction.user
        users_col = self.bot.users_col
        doc = await get_user(users_col, target.id, interaction.guild_id)

        # Fetch active title
        inv_doc = await self.bot.inv_col.find_one(
            {"user_id": target.id, "guild_id": interaction.guild_id}
        )
        active_title = inv_doc.get("active_title") if inv_doc else None

        # Rank
        activity_score = doc.get("voice_minutes", 0) + doc.get("messages_sent", 0)
        rank = get_rank(activity_score)

        # Streak
        streak = doc.get("streak", 0)
        best_streak = doc.get("streak_best", 0)

        description = (
            f"*{active_title}*"
            if active_title
            else "*drifting through hypnagogia, one dream at a time...*"
        )

        embed = discord.Embed(
            title=f"🌙  {target.display_name}",
            description=description,
            color=COLOUR_MAIN,
        )

        # ── Row 1: three stats — always fills a clean 3-column row ───────────
        embed.add_field(
            name="✨  Dream Points",
            value=f"```{doc['points']:,}```",
            inline=True,
        )
        embed.add_field(
            name="🎙️  Voice Time",
            value=f"```{_fmt_voice(doc.get('voice_minutes', 0))}```",
            inline=True,
        )
        embed.add_field(
            name="💬  Messages",
            value=f"```{doc.get('messages_sent', 0):,}```",
            inline=True,
        )

        # ── Divider ───────────────────────────────────────────────────────────
        embed.add_field(name=_BLANK, value="▸  **Rank & Activity**", inline=False)

        # ── Row 2: rank (full width) ──────────────────────────────────────────
        progress_bar = _progress_bar(rank["progress_pct"])
        embed.add_field(
            name=f"🏅  {rank['symbol']}  {rank['name']}",
            value=(
                f"{progress_bar}  `{rank['progress_pct']}%`\n"
                f"-# *{rank['points_to_next']:,} until  {rank['next_symbol']}  {rank['next_name']}*"
            ),
            inline=True,
        )

        # ── Row 3: streak + spacer to keep layout clean ───────────────────────
        if streak > 0:
            flame = "🔥" * min(streak // 7 + 1, 5)
            plural = "s" if streak != 1 else ""
            streak_val = (
                f"```{streak} day{plural}```{flame}\n-# *best: {best_streak} days*"
            )
        else:
            streak_val = f"```0 days```\n-# *start chatting to begin a streak!*"

        embed.add_field(name="📅  Streak", value=streak_val, inline=True)
        embed.add_field(name=_BLANK, value=_BLANK, inline=True)  # spacer

        # ── Comp roll counts (all-time) ──────────────────────────────────────
        ROLE_ICONS = {
            "Duelist": "⚔️",
            "Initiator": "🔍",
            "Controller": "💨",
            "Sentinel": "🛡️",
            "Free Pick": "🌀",
        }
        roll_docs = await self.bot.comp_rolls_col.find(
            {"guild_id": interaction.guild_id, "user_id": target.id}
        ).to_list(length=100)

        if roll_docs:
            # Sum counts per role across all weeks
            totals: dict[str, int] = {}
            for doc in roll_docs:
                totals[doc["role"]] = totals.get(doc["role"], 0) + doc["count"]

            role_order = ["Duelist", "Initiator", "Controller", "Sentinel", "Free Pick"]
            parts = [f"{ROLE_ICONS[r]} {totals[r]}" for r in role_order if r in totals]
            embed.add_field(
                name=_BLANK,
                value="▸  **Comp Rolls**",
                inline=False,
            )
            embed.add_field(
                name="🎲  Role History",
                value="  ".join(parts),
                inline=True,
            )
            embed.add_field(name=_BLANK, value=_BLANK, inline=True)
            embed.add_field(name=_BLANK, value=_BLANK, inline=True)

        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.response.send_message(embed=embed)


def _progress_bar(pct: int, length: int = 12) -> str:
    filled = round(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


def _fmt_voice(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


async def setup(bot: commands.Bot):
    await bot.add_cog(Points(bot))
