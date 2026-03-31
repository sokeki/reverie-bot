import discord
from discord import app_commands
from discord.ext import commands
import random

from config import COLOUR_MAIN, COLOUR_LB, COLOUR_CONFIRM


# ── Agent data ────────────────────────────────────────────────────────────────

AGENTS: dict[str, list[str]] = {
    "Duelist": [
        "Jett",
        "Reyna",
        "Raze",
        "Phoenix",
        "Yoru",
        "Neon",
        "Iso",
        "Waylay",
    ],
    "Initiator": [
        "Sova",
        "Breach",
        "Skye",
        "KAY/O",
        "Fade",
        "Gekko",
    ],
    "Controller": [
        "Brimstone",
        "Viper",
        "Omen",
        "Astra",
        "Harbor",
        "Clove",
    ],
    "Sentinel": [
        "Sage",
        "Cypher",
        "Killjoy",
        "Chamber",
        "Deadlock",
        "Vyse",
    ],
}

ROLE_COLOURS = {
    "Duelist": 0xFF4655,  # Valorant red
    "Initiator": 0x4CAF50,  # green
    "Controller": 0x9C27B0,  # purple
    "Sentinel": 0x2196F3,  # blue
}

ROLE_EMOJIS = {
    "Duelist": "⚔️",
    "Initiator": "🔍",
    "Controller": "🌫️",
    "Sentinel": "🛡️",
}

ALL_ROLES = list(AGENTS.keys())


# ── Cog ───────────────────────────────────────────────────────────────────────


class Valorant(commands.Cog):
    """Valorant random agent, role, and team comp commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /randomagent ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="randomagent",
        description="Get a random Valorant agent, optionally filtered by role",
    )
    @app_commands.describe(role="Filter by role (leave empty for any role)")
    @app_commands.choices(
        role=[
            app_commands.Choice(name="Duelist", value="Duelist"),
            app_commands.Choice(name="Initiator", value="Initiator"),
            app_commands.Choice(name="Controller", value="Controller"),
            app_commands.Choice(name="Sentinel", value="Sentinel"),
        ]
    )
    async def randomagent(
        self, interaction: discord.Interaction, role: app_commands.Choice[str] = None
    ):
        if role:
            pool = AGENTS[role.value]
            chosen = random.choice(pool)
            role_name = role.value
        else:
            role_name = random.choice(ALL_ROLES)
            pool = AGENTS[role_name]
            chosen = random.choice(pool)

        embed = discord.Embed(
            title=f"{ROLE_EMOJIS[role_name]}  {chosen}",
            description=f"**Role:** {role_name}",
            color=ROLE_COLOURS[role_name],
        )
        embed.set_footer(text="Reverie  •  Hypnogogia")
        await interaction.response.send_message(embed=embed)

    # ── /randomrole ───────────────────────────────────────────────────────────

    @app_commands.command(name="randomrole", description="Get a random Valorant role")
    async def randomrole(self, interaction: discord.Interaction):
        role = random.choice(ALL_ROLES)

        embed = discord.Embed(
            title=f"{ROLE_EMOJIS[role]}  {role}",
            description=f"*your role for this round*",
            color=ROLE_COLOURS[role],
        )
        embed.set_footer(text="Reverie  •  Hypnogogia")
        await interaction.response.send_message(embed=embed)

    # ── /randomcomp ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="randomcomp", description="Randomly assign 5 players to Valorant roles"
    )
    @app_commands.describe(
        player1="Player 1",
        player2="Player 2",
        player3="Player 3",
        player4="Player 4",
        player5="Player 5",
    )
    async def randomcomp(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member,
        player3: discord.Member,
        player4: discord.Member,
        player5: discord.Member,
    ):
        players = [player1, player2, player3, player4, player5]
        random.shuffle(players)

        # Assign roles — 4 fixed + 1 free pick
        fixed_roles = ALL_ROLES.copy()
        assignments = {}
        for i, role in enumerate(fixed_roles):
            assignments[role] = players[i]
        assignments["Free Pick"] = players[4]

        # Build ping list for the message content so players get notified
        pings = " ".join(p.mention for p in players)

        embed = discord.Embed(
            title="🎲 Random Team Comp",
            description="*roles have been assigned — good luck!*",
            color=COLOUR_LB,
        )

        role_order = ["Duelist", "Initiator", "Controller", "Sentinel", "Free Pick"]
        for role in role_order:
            emoji = ROLE_EMOJIS.get(role, "🎯")
            embed.add_field(
                name=f"{emoji}  {role}",
                value=assignments[role].mention,
                inline=True,
            )

        embed.set_footer(text="Reverie  •  Hypnogogia")
        await interaction.response.send_message(content=pings, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Valorant(bot))
