import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_LB


class Leaderboard(commands.Cog):
    """Handles the /leaderboard slash command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="leaderboard",
        description="See the Hall of Dreamers - top members by points",
    )
    @app_commands.describe(top="How many dreamers to show (3–25, default 10)")
    async def leaderboard(self, interaction: discord.Interaction, top: int = 10):
        top = min(max(top, 3), 25)
        users_col = self.bot.users_col

        docs = (
            await users_col.find({"guild_id": interaction.guild_id})
            .sort("points", -1)
            .limit(top)
            .to_list(length=top)
        )

        if not docs:
            await interaction.response.send_message(
                "*the dream is still empty...* no points yet - start chatting! 🌫️",
                ephemeral=True,
            )
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for rank, doc in enumerate(docs, start=1):
            m = interaction.guild.get_member(doc["user_id"])
            name = m.display_name if m else f"Dreamer {doc['user_id']}"
            medal = medals.get(rank, f"`#{rank}`")
            lines.append(f"{medal} **{name}** > {doc['points']:,} dream points")

        embed = discord.Embed(
            title="🌒 Hall of Dreamers",
            description="\n".join(lines),
            color=COLOUR_LB,
        )
        embed.set_footer(
            text=f"Top {top} dreamers  •  /points to check yourself  •  Reverie"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
