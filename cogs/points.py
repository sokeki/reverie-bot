import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_MAIN
from utils.db import get_user
from utils.ranks import get_rank


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

        # Fetch active title from inventory
        inv_doc = await self.bot.inv_col.find_one(
            {"user_id": target.id, "guild_id": interaction.guild_id}
        )
        active_title = inv_doc.get("active_title") if inv_doc else None

        # Calculate rank based on voice minutes + messages sent
        activity_score = doc.get("voice_minutes", 0) + doc.get("messages_sent", 0)
        rank = get_rank(activity_score)

        description = (
            f"*{active_title}*"
            if active_title
            else "*drifting through hypnagogia, one dream at a time...*"
        )

        embed = discord.Embed(
            title=f"🌙 {target.display_name}'s Reverie",
            description=description,
            color=COLOUR_MAIN,
        )
        embed.add_field(
            name="✨ Dream Points", value=f"`{doc['points']:,}`", inline=True
        )
        embed.add_field(
            name="🎙️ Voice Time",
            value=f"`{_fmt_voice(doc.get('voice_minutes', 0))}`",
            inline=True,
        )
        embed.add_field(
            name="💬 Messages Sent", value=f"`{doc['messages_sent']:,}`", inline=True
        )

        # Rank field
        rank_display = f"{rank['symbol']}  {rank['name']}"
        progress_bar = _progress_bar(rank["progress_pct"])
        rank_value = (
            f"`{rank_display}`\n"
            f"{progress_bar} `{rank['progress_pct']}%`\n"
            f"*{rank['points_to_next']:,} until {rank['next_symbol']} {rank['next_name']}*"
        )
        embed.add_field(name="🏅 Rank", value=rank_value, inline=False)

        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.response.send_message(embed=embed)


def _progress_bar(pct: int, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


def _fmt_voice(minutes: int) -> str:
    """Convert minutes to a human-readable hours/minutes string."""
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


async def setup(bot: commands.Bot):
    await bot.add_cog(Points(bot))
