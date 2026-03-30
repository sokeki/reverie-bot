import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_MAIN
from utils.db import get_user


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

        description = (
            f"*{active_title}*\n*drifting through hypnogogia, one dream at a time...*"
            if active_title
            else "*drifting through hypnogogia, one dream at a time...*"
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
            name="🎙️ Voice Minutes", value=f"`{doc['voice_minutes']:,}`", inline=True
        )
        embed.add_field(
            name="💬 Messages Sent", value=f"`{doc['messages_sent']:,}`", inline=True
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Reverie  •  Hypnogogia")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Points(bot))
