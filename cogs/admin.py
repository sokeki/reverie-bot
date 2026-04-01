import os
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import add_points
from config import COLOUR_MAIN


class Admin(commands.Cog):
    """Handles admin slash commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="addpoints", description="[Admin] Add or remove dream points from a member"
    )
    @app_commands.describe(
        member="Target member",
        amount="Points to add (use a negative number to remove)",
    )
    @app_commands.default_permissions(administrator=True)
    async def addpoints(
        self, interaction: discord.Interaction, member: discord.Member, amount: int
    ):
        await add_points(self.bot.users_col, member.id, interaction.guild_id, amount)
        sign = "+" if amount >= 0 else ""
        await interaction.response.send_message(
            f"🌙 {sign}{amount} dream points woven into **{member.display_name}**'s reverie.",
            ephemeral=True,
        )

    @app_commands.command(
        name="dashboard", description="Get the link to the Reverie dashboard"
    )
    async def dashboard(self, interaction: discord.Interaction):
        url = os.getenv("DISCORD_REDIRECT_URI", "").replace("/callback", "")
        if not url:
            await interaction.response.send_message(
                "⚠️ Dashboard URL is not configured.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🌙 Reverie Dashboard",
            description=f"View the leaderboard, shop, and server stats.\n\n{url}",
            color=COLOUR_MAIN,
            url=url,
        )
        embed.set_image(url=f"{url}/static/og-image.png")
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
