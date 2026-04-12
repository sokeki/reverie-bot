import discord
from discord import app_commands
from discord.ext import commands
import random

from config import COLOUR_LB


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
    "Duelist": 0xFF4655,
    "Initiator": 0x4CAF50,
    "Controller": 0x9C27B0,
    "Sentinel": 0x2196F3,
}

ROLE_EMOJIS = {
    "Duelist": "⚔️",
    "Initiator": "🔍",
    "Controller": "🌫️",
    "Sentinel": "🛡️",
    "Free Pick": "🎯",
}

ALL_ROLES = list(AGENTS.keys())
ALL_AGENTS = [agent for pool in AGENTS.values() for agent in pool]


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
            role_name = role.value
            chosen = random.choice(AGENTS[role_name])
        else:
            role_name = random.choice(ALL_ROLES)
            chosen = random.choice(AGENTS[role_name])

        embed = discord.Embed(
            title=f"{ROLE_EMOJIS[role_name]}  {chosen}",
            description=f"**Role:** {role_name}",
            color=ROLE_COLOURS[role_name],
        )
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.response.send_message(embed=embed)

    # ── /randomrole ───────────────────────────────────────────────────────────

    @app_commands.command(name="randomrole", description="Get a random Valorant role")
    async def randomrole(self, interaction: discord.Interaction):
        role = random.choice(ALL_ROLES)

        embed = discord.Embed(
            title=f"{ROLE_EMOJIS[role]}  {role}",
            description="*your role for this round*",
            color=ROLE_COLOURS[role],
        )
        embed.set_footer(text="Reverie  •  Hypnagogia")
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
        roll_agents="Also roll a random agent for each player",
    )
    async def randomcomp(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member,
        player3: discord.Member,
        player4: discord.Member,
        player5: discord.Member,
        roll_agents: bool = False,
    ):
        players = [player1, player2, player3, player4, player5]
        random.shuffle(players)

        role_order = ["Duelist", "Initiator", "Controller", "Sentinel", "Free Pick"]

        # Assign roles
        assignments: dict[str, discord.Member] = {}
        for i, role in enumerate(ALL_ROLES):
            assignments[role] = players[i]
        assignments["Free Pick"] = players[4]

        # Optionally roll agents
        rolled_agents: dict[str, str] = {}
        if roll_agents:
            for role in ALL_ROLES:
                rolled_agents[role] = random.choice(AGENTS[role])
            rolled_agents["Free Pick"] = random.choice(ALL_AGENTS)

        # Build one line per role
        lines = []
        for role in role_order:
            emoji = ROLE_EMOJIS[role]
            member = assignments[role].mention
            if roll_agents:
                agent = rolled_agents[role]
                lines.append(f"{emoji} **{role}** — {member}  ›  *{agent}*")
            else:
                lines.append(f"{emoji} **{role}** — {member}")

        pings = " ".join(p.mention for p in players)

        embed = discord.Embed(
            title="🎲 Random Team Comp",
            description="\n".join(lines),
            color=COLOUR_LB,
        )
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.response.send_message(content=pings, embed=embed)

        # Record role assignments for weekly recap stats
        from datetime import datetime, timezone

        week = (
            datetime.now(timezone.utc)
            - __import__("datetime").timedelta(
                days=(datetime.now(timezone.utc).weekday() + 1) % 7
            )
        ).strftime("%Y-%m-%d")
        for role, member in assignments.items():
            await self.bot.comp_rolls_col.update_one(
                {
                    "guild_id": interaction.guild_id,
                    "user_id": member.id,
                    "week": week,
                    "role": role,
                },
                {"$inc": {"count": 1}},
                upsert=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Valorant(bot))
