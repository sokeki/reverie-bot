import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_LB
from utils.ranks import get_rank


def _fmt_voice(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


SORT_FIELDS = {
    "points": ("points", "✨ Dream Points"),
    "rank": ("voice_minutes", "🏅 Rank"),  # rank is derived, sort by activity score
    "voice": ("voice_minutes", "🎙️ Voice Time"),
    "messages": ("messages_sent", "💬 Messages"),
}


class Leaderboard(commands.Cog):
    """Handles the /leaderboard slash command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="leaderboard",
        description="See the Hall of Dreamers  •  top members by points",
    )
    @app_commands.describe(
        top="How many dreamers to show (3–25, default 10)",
        sort="Sort by (default: points)",
    )
    @app_commands.choices(
        sort=[
            app_commands.Choice(name="✨ Dream Points", value="points"),
            app_commands.Choice(name="🏅 Rank", value="rank"),
            app_commands.Choice(name="🎙️ Voice Time", value="voice"),
            app_commands.Choice(name="💬 Messages", value="messages"),
        ]
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        top: int = 10,
        sort: app_commands.Choice[str] = None,
    ):
        top = min(max(top, 3), 25)
        sort_key = sort.value if sort else "points"
        db_field, label = SORT_FIELDS[sort_key]

        docs = (
            await self.bot.users_col.find({"guild_id": interaction.guild_id})
            .sort(db_field, -1)
            .limit(top)
            .to_list(length=top)
        )

        # For rank sort, re-sort by activity score (voice_minutes + messages_sent)
        if sort_key == "rank":
            docs.sort(
                key=lambda d: d.get("voice_minutes", 0) + d.get("messages_sent", 0),
                reverse=True,
            )

        if not docs:
            await interaction.response.send_message(
                "*the dream is still empty...* no points yet - start chatting! 🌫️",
                ephemeral=True,
            )
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for position, doc in enumerate(docs, start=1):
            m = interaction.guild.get_member(doc["user_id"])
            name = m.display_name if m else f"Dreamer {doc['user_id']}"
            medal = medals.get(position, f"`#{position}`")

            if sort_key == "points":
                value = f"{doc.get('points', 0):,} pts"
            elif sort_key == "rank":
                score = doc.get("voice_minutes", 0) + doc.get("messages_sent", 0)
                r = get_rank(score)
                value = f"{r['symbol']} {r['name']}"
            elif sort_key == "voice":
                value = _fmt_voice(doc.get("voice_minutes", 0))
            else:
                value = f"{doc.get('messages_sent', 0):,} messages"

            lines.append(f"{medal} **{name}**  •  {value}")

        embed = discord.Embed(
            title=f"🌒 Hall of Dreamers  •  {label}",
            description="\n".join(lines),
            color=COLOUR_LB,
        )
        embed.set_footer(
            text=f"Top {top} dreamers  •  /points to check yourself  •  Reverie  •  {interaction.guild.name}"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
