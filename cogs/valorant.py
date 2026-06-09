import discord
from discord import app_commands
from discord.ext import commands
import random
from datetime import datetime, timezone, timedelta

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
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
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
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
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
        await interaction.response.defer()
        players = [player1, player2, player3, player4, player5]

        # ── Fetch active comp items for all players ────────────────────────
        # Active items are stored on the user doc under "active_comp_item"
        # { "type": "comp_role_lock"|"comp_role_ban"|"comp_agent_lock"|"comp_reroll",
        #   "value": "<role or agent name>" }
        # We read them now, consume them after we've confirmed they're valid.

        active_items: dict[int, dict] = {}  # user_id -> active item doc
        for player in players:
            user_doc = await self.bot.users_col.find_one(
                {"user_id": player.id, "guild_id": interaction.guild_id}
            )
            if user_doc and user_doc.get("active_comp_item"):
                active_items[player.id] = user_doc["active_comp_item"]

        # ── Resolve locks first ───────────────────────────────────────────
        # locked_roles: role -> player  (role locks and agent locks both claim a role)
        # locked_agents: player_id -> agent
        locked_roles: dict[str, discord.Member] = {}  # Valorant role -> player
        locked_agents: dict[int, str] = {}  # player_id -> agent
        banned_roles: dict[int, str] = {}  # player_id -> banned role

        item_notes: list[str] = []  # shown under the comp result

        for player in players:
            item = active_items.get(player.id)
            if not item:
                continue
            itype = item.get("type")
            value = item.get("value", "")

            if itype == "comp_role_lock":
                if value in ALL_ROLES and value not in locked_roles:
                    locked_roles[value] = player
                    item_notes.append(f"🎯 {player.mention} locked **{value}**")
                # If role already claimed by another lock, silently ignore (first come first served)

            elif itype == "comp_agent_lock":
                # Find which role the agent belongs to
                agent_role = next(
                    (r for r, agents in AGENTS.items() if value in agents), None
                )
                if agent_role and agent_role not in locked_roles:
                    locked_roles[agent_role] = player
                    locked_agents[player.id] = value
                    item_notes.append(
                        f"🌟 {player.mention} locked **{value}** ({agent_role})"
                    )

            elif itype == "comp_role_ban":
                if value in ALL_ROLES:
                    banned_roles[player.id] = value
                    item_notes.append(f"🚫 {player.mention} banned **{value}**")

        # ── Assign remaining players to remaining roles ───────────────────
        unassigned_players = [
            p for p in players if p.id not in {m.id for m in locked_roles.values()}
        ]
        remaining_roles = [r for r in ALL_ROLES if r not in locked_roles]

        random.shuffle(unassigned_players)
        random.shuffle(remaining_roles)

        # Build a list of (player, eligible_roles) respecting bans
        # Greedy assignment: sort by fewest eligible roles first to avoid impossible states
        player_eligible: list[tuple[discord.Member, list[str]]] = []
        for player in unassigned_players:
            banned = banned_roles.get(player.id)
            eligible = [r for r in remaining_roles if r != banned]
            player_eligible.append((player, eligible))

        player_eligible.sort(key=lambda x: len(x[1]))

        assignments: dict[str, discord.Member] = dict(locked_roles)
        taken_roles: set[str] = set(locked_roles.keys())

        for player, eligible in player_eligible:
            available = [r for r in eligible if r not in taken_roles]
            if available:
                chosen_role = random.choice(available)
            else:
                # All eligible roles taken (e.g. ban made it impossible) — pick any remaining
                fallback = [r for r in remaining_roles if r not in taken_roles]
                chosen_role = random.choice(fallback) if fallback else None

            if chosen_role:
                assignments[chosen_role] = player
                taken_roles.add(chosen_role)
            # If somehow no role left (shouldn't happen with 5 players / 4 roles), they get Free Pick

        # Fifth player always gets Free Pick (the one not assigned a named role)
        assigned_players = set(m.id for m in assignments.values())
        free_pick_player = next(
            (p for p in players if p.id not in assigned_players), players[4]
        )
        assignments["Free Pick"] = free_pick_player

        # ── Roll agents (no repeats) ──────────────────────────────────────
        rolled_agents: dict[str, str] = {}
        if roll_agents:
            # Remove any locked agents from the pool before dealing
            locked_agent_names = set(locked_agents.values())
            pool: list[str] = [a for a in ALL_AGENTS if a not in locked_agent_names]
            random.shuffle(pool)

            for role in ALL_ROLES:
                player = assignments.get(role)
                if player and player.id in locked_agents:
                    rolled_agents[role] = locked_agents[player.id]
                else:
                    # Draw from pool, staying within the correct role's agents where possible
                    role_pool = [a for a in pool if a in AGENTS[role]]
                    if role_pool:
                        chosen_agent = role_pool[0]
                        pool.remove(chosen_agent)
                    elif pool:
                        # All role-specific agents already used; draw any remaining
                        chosen_agent = pool.pop(0)
                    else:
                        chosen_agent = random.choice(
                            AGENTS[role]
                        )  # fallback (shouldn't happen)
                    rolled_agents[role] = chosen_agent

            # Free Pick draws from whatever is left in the pool (any role)
            if pool:
                rolled_agents["Free Pick"] = pool.pop(0)
            else:
                rolled_agents["Free Pick"] = random.choice(ALL_AGENTS)

        # ── Consume active comp items ─────────────────────────────────────
        for player in players:
            if player.id in active_items:
                itype = active_items[player.id].get("type")
                # Remove item from inventory and clear active_comp_item
                await self.bot.inv_col.update_one(
                    {"user_id": player.id, "guild_id": interaction.guild_id},
                    {"$pull": {"items": {"type": itype}}},
                )
                await self.bot.users_col.update_one(
                    {"user_id": player.id, "guild_id": interaction.guild_id},
                    {"$unset": {"active_comp_item": ""}},
                )

        # ── Build embed ───────────────────────────────────────────────────
        role_order = ["Duelist", "Initiator", "Controller", "Sentinel", "Free Pick"]
        lines = []
        for role in role_order:
            emoji = ROLE_EMOJIS[role]
            member = assignments[role].mention
            if roll_agents:
                agent = rolled_agents[role]
                lines.append(f"{emoji} **{role}** — {member}  ›  *{agent}*")
            else:
                lines.append(f"{emoji} **{role}** — {member}")

        description = "\n".join(lines)
        if item_notes:
            description += "\n\n" + "\n".join(item_notes)

        pings = " ".join(p.mention for p in players)
        embed = discord.Embed(
            title="🎲 Random Team Comp",
            description=description,
            color=COLOUR_LB,
        )
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
        await interaction.followup.send(content=pings, embed=embed)

        # ── Record for weekly recap ───────────────────────────────────────
        now = datetime.now(timezone.utc)
        week = (now - timedelta(days=(now.weekday() + 1) % 7)).strftime("%Y-%m-%d")
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
            await self.bot.users_col.update_one(
                {"guild_id": interaction.guild_id, "user_id": member.id},
                {"$inc": {f"comp_roles.{role}": 1}},
                upsert=True,
            )

    # ── /useitem ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="useitem",
        description="Activate a comp item from your inventory before the next /randomcomp",
    )
    async def useitem(self, interaction: discord.Interaction):
        COMP_TYPES = {
            "comp_role_lock",
            "comp_role_ban",
            "comp_agent_lock",
            "comp_reroll",
        }
        LABEL_MAP = {
            "comp_role_lock": "🎯 Role Lock",
            "comp_role_ban": "🚫 Role Ban",
            "comp_agent_lock": "🌟 Agent Lock",
            "comp_reroll": "🔄 Role Reroll",
        }

        # ── inner: show the item selector ────────────────────────────────────
        async def _show_item_select(target: discord.Interaction):
            inv = await self.bot.inv_col.find_one(
                {"user_id": target.user.id, "guild_id": target.guild_id}
            )
            fresh_items = [
                i
                for i in (inv.get("items", []) if inv else [])
                if i["type"] in COMP_TYPES
            ]

            if not fresh_items:
                msg = "*you don't have any comp items.* Pick some up in the shop! 🛒"
                if target.response.is_done():
                    await target.edit_original_response(content=msg, view=None)
                else:
                    await target.response.send_message(msg, ephemeral=True)
                return

            seen_types: set[str] = set()
            options = []
            for item in fresh_items:
                key = item["type"]
                if key in seen_types:
                    continue
                seen_types.add(key)
                options.append(
                    discord.SelectOption(
                        label=LABEL_MAP.get(key, key),
                        value=key,
                        description=item.get("name", ""),
                    )
                )

            select = discord.ui.Select(
                placeholder="Choose which item to activate...", options=options
            )

            async def on_type_select(sel: discord.Interaction):
                chosen_type = select.values[0]

                if chosen_type == "comp_reroll":
                    await self.bot.users_col.update_one(
                        {"user_id": sel.user.id, "guild_id": sel.guild_id},
                        {
                            "$set": {
                                "active_comp_item": {"type": "comp_reroll", "value": ""}
                            }
                        },
                        upsert=True,
                    )
                    await sel.response.edit_message(
                        content="🔄 **Role Reroll** queued! It will activate on the next `/randomcomp` you're in.",
                        view=None,
                    )
                    return

                if chosen_type in ("comp_role_lock", "comp_role_ban"):
                    role_options = [
                        discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                        for r in ALL_ROLES
                    ]
                    role_select = discord.ui.Select(
                        placeholder="Choose a role...", options=role_options
                    )

                    async def on_role_select(rs: discord.Interaction):
                        chosen_role = role_select.values[0]
                        verb = "locked" if chosen_type == "comp_role_lock" else "banned"
                        emoji = "🎯" if chosen_type == "comp_role_lock" else "🚫"
                        await self.bot.users_col.update_one(
                            {"user_id": rs.user.id, "guild_id": rs.guild_id},
                            {
                                "$set": {
                                    "active_comp_item": {
                                        "type": chosen_type,
                                        "value": chosen_role,
                                    }
                                }
                            },
                            upsert=True,
                        )
                        await rs.response.edit_message(
                            content=f"{emoji} **{chosen_role}** {verb}! It will apply on the next `/randomcomp` you're in.",
                            view=None,
                        )

                    role_select.callback = on_role_select
                    v = discord.ui.View(timeout=60)
                    v.add_item(role_select)
                    prompt = (
                        "🎯 Choose the role to **lock**:"
                        if chosen_type == "comp_role_lock"
                        else "🚫 Choose the role to **ban**:"
                    )
                    await sel.response.edit_message(content=prompt, view=v)
                    return

                if chosen_type == "comp_agent_lock":
                    role_options = [
                        discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                        for r in ALL_ROLES
                    ]
                    role_select = discord.ui.Select(
                        placeholder="First, choose an agent role...",
                        options=role_options,
                    )

                    async def on_agent_role_select(ars: discord.Interaction):
                        chosen_role = role_select.values[0]
                        agent_select = discord.ui.Select(
                            placeholder=f"Choose a {chosen_role} agent...",
                            options=[
                                discord.SelectOption(label=a, value=a)
                                for a in AGENTS[chosen_role]
                            ],
                        )

                        async def on_agent_select(as_: discord.Interaction):
                            chosen_agent = agent_select.values[0]
                            await self.bot.users_col.update_one(
                                {"user_id": as_.user.id, "guild_id": as_.guild_id},
                                {
                                    "$set": {
                                        "active_comp_item": {
                                            "type": "comp_agent_lock",
                                            "value": chosen_agent,
                                        }
                                    }
                                },
                                upsert=True,
                            )
                            await as_.response.edit_message(
                                content=f"🌟 **{chosen_agent}** locked! You'll get {chosen_role} + {chosen_agent} on the next `/randomcomp` you're in.",
                                view=None,
                            )

                        agent_select.callback = on_agent_select
                        v = discord.ui.View(timeout=60)
                        v.add_item(agent_select)
                        await ars.response.edit_message(
                            content=f"🌟 Choose your **{chosen_role}** agent:", view=v
                        )

                    role_select.callback = on_agent_role_select
                    v = discord.ui.View(timeout=60)
                    v.add_item(role_select)
                    await sel.response.edit_message(
                        content="🌟 Choose the **role** first, then pick your agent:",
                        view=v,
                    )

            select.callback = on_type_select
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            if target.response.is_done():
                await target.edit_original_response(
                    content="*choose a comp item to activate:*", view=view
                )
            else:
                await target.response.send_message(
                    content="*choose a comp item to activate:*",
                    view=view,
                    ephemeral=True,
                )

        # ── check for existing queued item ────────────────────────────────────
        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )

        if user_doc and user_doc.get("active_comp_item"):
            active = user_doc["active_comp_item"]
            itype = active.get("type", "")
            ivalue = active.get("value", "")
            active_label = LABEL_MAP.get(itype, itype)
            active_desc = f"**{ivalue}**" if ivalue else "no value set"

            replace_btn = discord.ui.Button(
                label="🔄 Replace with a different item",
                style=discord.ButtonStyle.primary,
                row=0,
            )
            cancel_btn = discord.ui.Button(
                label="❌ Cancel queued item", style=discord.ButtonStyle.danger, row=0
            )
            dismiss_btn = discord.ui.Button(
                label="✖ Dismiss", style=discord.ButtonStyle.secondary, row=0
            )

            async def on_replace(btn: discord.Interaction):
                await self.bot.users_col.update_one(
                    {"user_id": btn.user.id, "guild_id": btn.guild_id},
                    {"$unset": {"active_comp_item": ""}},
                )
                await _show_item_select(btn)

            async def on_cancel_queued(btn: discord.Interaction):
                await self.bot.users_col.update_one(
                    {"user_id": btn.user.id, "guild_id": btn.guild_id},
                    {"$unset": {"active_comp_item": ""}},
                )
                await btn.response.edit_message(
                    content=f"❌ Your queued **{active_label}** ({active_desc}) has been cancelled — it's still in your inventory.",
                    view=None,
                )

            async def on_dismiss(btn: discord.Interaction):
                await btn.response.edit_message(
                    content=f"*keeping your queued **{active_label}** ({active_desc}).*",
                    view=None,
                )

            replace_btn.callback = on_replace
            cancel_btn.callback = on_cancel_queued
            dismiss_btn.callback = on_dismiss

            conflict_view = discord.ui.View(timeout=60)
            conflict_view.add_item(replace_btn)
            conflict_view.add_item(cancel_btn)
            conflict_view.add_item(dismiss_btn)

            await interaction.response.send_message(
                f"⚠️ You already have **{active_label}** queued"
                + (f" → {active_desc}" if ivalue else "")
                + ".\nIt will activate on the next `/randomcomp` you're in.\n\nWhat would you like to do?",
                view=conflict_view,
                ephemeral=True,
            )
            return

        await _show_item_select(interaction)

    # ── /cancelitem ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="cancelitem",
        description="Cancel your currently queued comp item",
    )
    async def cancelitem(self, interaction: discord.Interaction):
        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        active = user_doc.get("active_comp_item") if user_doc else None
        if not active:
            await interaction.response.send_message(
                "*you don't have any comp item queued.*",
                ephemeral=True,
            )
            return

        LABEL_MAP = {
            "comp_role_lock": "🎯 Role Lock",
            "comp_role_ban": "🚫 Role Ban",
            "comp_agent_lock": "🌟 Agent Lock",
            "comp_reroll": "🔄 Role Reroll",
        }
        itype = active.get("type", "")
        ivalue = active.get("value", "")
        active_label = LABEL_MAP.get(itype, itype)

        await self.bot.users_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {"$unset": {"active_comp_item": ""}},
        )
        await interaction.response.send_message(
            f"❌ Queued **{active_label}"
            + (f" → {ivalue}" if ivalue else "")
            + "** cancelled — it's still in your inventory.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Valorant(bot))
