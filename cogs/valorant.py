"""
cogs/valorant.py
─────────────────────────────────────────────────────────────────────────────
Valorant random comp, agent, role commands + comp item system.

Comp item data model (on users_col doc):
  active_comp_item:    dict  — single queued item (lock / ban / swap / reroll)
  active_comp_weights: list  — stacked own-weight items  [{"value","weight","item_name"}]
  active_comp_curses:  list  — curses to apply to others [{"target_id","value","weight","item_name"}]

Items are consumed only inside /randomcomp.
/randomagent, /randomrole, /randomcomp (on start) never touch these.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_LB, COLOUR_MAIN

# ── Agent data ────────────────────────────────────────────────────────────────

AGENTS: dict[str, list[str]] = {
    "Duelist": ["Jett", "Reyna", "Raze", "Phoenix", "Yoru", "Neon", "Iso", "Waylay"],
    "Initiator": ["Sova", "Breach", "Skye", "KAY/O", "Fade", "Gekko"],
    "Controller": ["Brimstone", "Viper", "Omen", "Astra", "Harbor", "Clove"],
    "Sentinel": ["Sage", "Cypher", "Killjoy", "Chamber", "Deadlock", "Vyse"],
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

COMP_TYPES = {
    "comp_role_lock",
    "comp_role_ban",
    "comp_agent_lock",
    "comp_reroll",
    "comp_role_swap",
    "comp_weight",
    "comp_curse",
    "comp_reduce",
    "comp_curse_reduce",
}

LABEL_MAP = {
    "comp_role_lock": "🎯 Role Lock",
    "comp_role_ban": "🚫 Role Ban",
    "comp_agent_lock": "🌟 Agent Lock",
    "comp_reroll": "🔄 Role Reroll",
    "comp_role_swap": "🔀 Role Swap",
    "comp_weight": "⚖️ Role Weight",
    "comp_curse": "💀 Role Curse",
    "comp_reduce": "⬇️ Role Reduction",
    "comp_curse_reduce": "🪄 Curse Reduction",
}

MAX_WEIGHT = 100  # total stacked weight cap — at 100% unweighted roles have 0 chance

# ── Helpers ───────────────────────────────────────────────────────────────────


def _multi_weighted_choice(available: list[str], role_deltas: dict[str, float]) -> str:
    """
    Draw a role from available using a delta map.
    Each role starts at equal baseline (100 / len(available)).
    Positive deltas boost probability, negative deltas reduce it.
    Result is floored at 0 per role then renormalised.
    """
    n = len(available)
    if n == 0:
        raise ValueError("available is empty")
    baseline = 100.0 / n
    raw = {r: max(0.0, baseline + role_deltas.get(r, 0.0)) for r in available}
    total = sum(raw.values())
    if total <= 0:
        return random.choice(available)
    weights = [raw[r] / total for r in available]
    return random.choices(available, weights=weights, k=1)[0]


def _build_comp_embed(
    assignments: dict[str, discord.Member],
    rolled_agents: dict[str, str],
    item_notes: list[str],
    swap_players: set[int],
    reroll_players: set[int],
    guild_name: str,
) -> discord.Embed:
    role_order = ["Duelist", "Initiator", "Controller", "Sentinel", "Free Pick"]
    lines = []
    for role in role_order:
        member = assignments.get(role)
        if not member:
            continue
        emoji = ROLE_EMOJIS[role]
        mention = member.mention
        if rolled_agents and role in rolled_agents:
            lines.append(f"{emoji} **{role}** — {mention}  ›  *{rolled_agents[role]}*")
        else:
            lines.append(f"{emoji} **{role}** — {mention}")

    description = "\n".join(lines)
    if item_notes:
        description += "\n\n" + "\n".join(item_notes)
    if swap_players:
        description += "\n*🔀 Role Swap holders — click the button below to swap.*"
    if reroll_players:
        description += "\n*🔄 Reshuffle holders — click the button below to reshuffle all unlocked roles.*"

    embed = discord.Embed(
        title="🎲 Random Team Comp", description=description, color=COLOUR_LB
    )
    embed.set_footer(text=f"Reverie  •  {guild_name}")
    return embed


# ── Comp post-roll view (swap + reroll buttons) ───────────────────────────────


class CompPostView(discord.ui.View):
    """Shown on the comp result. Holds swap and/or reshuffle buttons."""

    def __init__(
        self,
        assignments,
        swap_player_ids,
        reroll_player_ids,
        locked_player_ids,
        guild_id,
        bot,
        roll_agents,
        rolled_agents,
    ):
        super().__init__(timeout=120)
        self.assignments = assignments
        self.swap_player_ids = swap_player_ids
        self.reroll_player_ids = reroll_player_ids
        self.locked_player_ids = locked_player_ids
        self.guild_id = guild_id
        self.bot = bot
        self.roll_agents = roll_agents
        self.rolled_agents = rolled_agents
        self.role_of: dict[int, str] = {m.id: r for r, m in assignments.items()}
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        if self.swap_player_ids:
            btn = discord.ui.Button(
                label="🔀 Use Role Swap", style=discord.ButtonStyle.primary
            )
            btn.callback = self._on_swap
            self.add_item(btn)
        if self.reroll_player_ids:
            btn = discord.ui.Button(
                label="🔄 Reshuffle", style=discord.ButtonStyle.secondary
            )
            btn.callback = self._on_reroll
            self.add_item(btn)

    def _current_embed(self, extra_note: str = "") -> discord.Embed:
        embed = _build_comp_embed(
            self.assignments,
            self.rolled_agents,
            [],
            self.swap_player_ids,
            self.reroll_player_ids,
            "",
        )
        if extra_note:
            embed.description += f"\n\n{extra_note}"
        return embed

    async def _on_swap(self, interaction: discord.Interaction):
        if interaction.user.id not in self.swap_player_ids:
            await interaction.response.send_message(
                "⚠️ This swap isn't for you.", ephemeral=True
            )
            return
        my_role = self.role_of.get(interaction.user.id)
        if not my_role:
            await interaction.response.send_message(
                "⚠️ Couldn't find your role.", ephemeral=True
            )
            return
        options = [
            discord.SelectOption(
                label=f"{m.display_name}  ({r})",
                value=str(m.id),
                emoji=ROLE_EMOJIS.get(r, "•"),
            )
            for r, m in self.assignments.items()
            if m.id != interaction.user.id
        ]
        select = discord.ui.Select(placeholder="Swap with...", options=options)
        original_message = (
            interaction.message
        )  # comp embed — store before sending ephemeral

        async def on_pick(sel: discord.Interaction):
            tid = int(select.values[0])
            t_role = self.role_of.get(tid)
            if not t_role:
                await sel.response.send_message(
                    "⚠️ Couldn't find that player's role.", ephemeral=True
                )
                return
            t_member = self.assignments[t_role]
            my_member = self.assignments[my_role]
            self.assignments[my_role] = t_member
            self.assignments[t_role] = my_member
            self.role_of[my_member.id] = t_role
            self.role_of[t_member.id] = my_role
            self.swap_player_ids.discard(sel.user.id)
            self._rebuild()
            note = f"🔀 *{sel.user.display_name} swapped **{my_role}** ↔ {t_member.display_name} (**{t_role}**)*"
            # Respond first (required by Discord), then edit the comp embed, then consume
            await sel.response.send_message("🔀 Swapped!", ephemeral=True)
            await original_message.edit(
                embed=self._current_embed(note), view=self if self.children else None
            )
            await self.bot.inv_col.update_one(
                {"user_id": sel.user.id, "guild_id": self.guild_id},
                {"$pull": {"items": {"type": "comp_role_swap"}}},
            )
            await self.bot.users_col.update_one(
                {"user_id": sel.user.id, "guild_id": self.guild_id},
                {"$unset": {"active_comp_item": ""}},
            )

        select.callback = on_pick
        v = discord.ui.View(timeout=60)
        v.add_item(select)
        await interaction.response.send_message(
            f"*You have **{my_role}**. Who do you want to swap with?*",
            view=v,
            ephemeral=True,
        )

    async def _on_reroll(self, interaction: discord.Interaction):
        if interaction.user.id not in self.reroll_player_ids:
            await interaction.response.send_message(
                "⚠️ This reshuffle isn't for you.", ephemeral=True
            )
            return

        # Keep locked players in place; reshuffle everyone else
        free_roles = [
            r for r, m in self.assignments.items() if m.id not in self.locked_player_ids
        ]
        free_members = [self.assignments[r] for r in free_roles]
        random.shuffle(free_members)
        for role, member in zip(free_roles, free_members):
            self.assignments[role] = member
            self.role_of[member.id] = role

        # Re-roll agents for reshuffled slots
        if self.roll_agents and self.rolled_agents:
            locked_agent_vals = {
                self.rolled_agents[r]
                for r in self.rolled_agents
                if self.assignments.get(r)
                and self.assignments[r].id in self.locked_player_ids
            }
            pool = [a for a in ALL_AGENTS if a not in locked_agent_vals]
            random.shuffle(pool)
            for role in free_roles:
                if role == "Free Pick":
                    self.rolled_agents[role] = (
                        pool.pop(0) if pool else random.choice(ALL_AGENTS)
                    )
                else:
                    role_pool = [a for a in pool if a in AGENTS.get(role, [])]
                    if role_pool:
                        chosen = role_pool[0]
                        pool.remove(chosen)
                    elif pool:
                        chosen = pool.pop(0)
                    else:
                        chosen = random.choice(AGENTS.get(role, ALL_AGENTS))
                    self.rolled_agents[role] = chosen

        await self.bot.inv_col.update_one(
            {"user_id": interaction.user.id, "guild_id": self.guild_id},
            {"$pull": {"items": {"type": "comp_reroll"}}},
        )
        await self.bot.users_col.update_one(
            {"user_id": interaction.user.id, "guild_id": self.guild_id},
            {"$unset": {"active_comp_item": ""}},
        )
        self.reroll_player_ids.discard(interaction.user.id)
        self._rebuild()

        locked_names = (
            ", ".join(
                self.assignments[r].display_name
                for r in self.assignments
                if self.assignments[r].id in self.locked_player_ids
            )
            or "none"
        )
        note = f"🔄 *{interaction.user.display_name} reshuffled all unlocked roles  •  protected: {locked_names}*"
        comp_message = interaction.message
        await interaction.response.send_message("🔄 Reshuffled!", ephemeral=True)
        await comp_message.edit(
            embed=self._current_embed(note), view=self if self.children else None
        )

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True


class PreRollView(discord.ui.View):
    """
    Shown to the /randomcomp invoker before rolling.
    Lists comp items in their inventory and lets them activate any.
    Pressing Roll! triggers the actual comp.
    """

    def __init__(
        self,
        cog: "Valorant",
        players: list[discord.Member],
        roll_agents: bool,
        invoker: discord.Member,
        guild_id: int,
    ):
        super().__init__(timeout=120)
        self.cog = cog
        self.players = players
        self.roll_agents = roll_agents
        self.invoker = invoker
        self.guild_id = guild_id
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        roll_btn = discord.ui.Button(
            label="🎲 Roll!", style=discord.ButtonStyle.success, row=0
        )
        roll_btn.callback = self._roll
        self.add_item(roll_btn)

        use_btn = discord.ui.Button(
            label="🎒 Use an item", style=discord.ButtonStyle.secondary, row=0
        )
        use_btn.callback = self._use_item
        self.add_item(use_btn)

        cancel_btn = discord.ui.Button(
            label="✖ Nevermind", style=discord.ButtonStyle.secondary, row=0
        )
        cancel_btn.callback = self._cancel
        self.add_item(cancel_btn)

    async def _build_status_embed(self) -> discord.Embed:
        """Show what's currently queued for the invoker."""
        user_doc = await self.cog.bot.users_col.find_one(
            {"user_id": self.invoker.id, "guild_id": self.guild_id}
        )
        inv = await self.cog.bot.inv_col.find_one(
            {"user_id": self.invoker.id, "guild_id": self.guild_id}
        )
        inv_items = (inv or {}).get("items", [])
        lines = []
        active = (user_doc or {}).get("active_comp_item")
        if active:
            label = LABEL_MAP.get(active.get("type", ""), "?")
            val = active.get("value", "")
            lines.append(f"• {label}" + (f": **{val}**" if val else ""))

        weights = (user_doc or {}).get("active_comp_weights", [])
        if weights:
            # Group by role and sum
            role_totals: dict[str, int] = {}
            for w in weights:
                r = w.get("role") or w.get("value", "?")
                role_totals[r] = role_totals.get(r, 0) + w["weight"]
            pool_total = min(sum(role_totals.values()), MAX_WEIGHT)
            breakdown = ", ".join(f"**{r}** +{v}%" for r, v in role_totals.items())
            lines.append(f"• ⚖️ Weight: {breakdown}  ({pool_total}% pool used)")

        reductions = (user_doc or {}).get("active_comp_reductions", [])
        if reductions:
            role_totals_r: dict[str, int] = {}
            for rd in reductions:
                r = rd.get("role") or rd.get("value", "?")
                role_totals_r[r] = role_totals_r.get(r, 0) + rd["weight"]
            breakdown = ", ".join(f"**{r}** -{v}%" for r, v in role_totals_r.items())
            lines.append(f"• ⬇️ Reduce: {breakdown}")

        curses = (user_doc or {}).get("active_comp_curses", [])
        for c in curses:
            target = discord.utils.get(self.players, id=c["target_id"])
            tname = target.display_name if target else f"<{c['target_id']}>"
            role = c.get("role") or c.get("value", "?")
            lines.append(f"• 💀 Curse → {tname}: **{role}** +{c['weight']}%")

        curse_reds = (user_doc or {}).get("active_comp_curse_reds", [])
        for cr in curse_reds:
            target = discord.utils.get(self.players, id=cr["target_id"])
            tname = target.display_name if target else f"<{cr['target_id']}>"
            role = cr.get("role") or cr.get("value", "?")
            lines.append(f"• 🪄 Curse Reduce → {tname}: **{role}** -{cr['weight']}%")

        # Show auto-detected reroll/swap from inventory (no activation needed)
        for itype, label_icon in (("comp_reroll", "🔄"), ("comp_role_swap", "🔀")):
            if not active or active.get("type") != itype:
                match = next((i for i in inv_items if i["type"] == itype), None)
                if match:
                    lines.append(
                        f"• {label_icon} **{LABEL_MAP[itype]}** ready *(auto-activates)*"
                    )

        desc = "\n".join(lines) if lines else "*no items queued — rolling clean*"
        embed = discord.Embed(
            title="🎲 Ready to roll?",
            description=f"**Players:** {', '.join(p.display_name for p in self.players)}\n\n**Your queued items:**\n{desc}",
            color=COLOUR_MAIN,
        )
        embed.set_footer(text="Activate items with 🎒, then press 🎲 Roll! when ready.")
        return embed

    async def _roll(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message(
                "Only the person who ran the comp can roll.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content="*rolling...*", embed=None, view=None
        )
        await self.cog._execute_comp(interaction, self.players, self.roll_agents)

    async def _use_item(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message(
                "Only the person who ran the comp can use items.", ephemeral=True
            )
            return
        await self.cog._show_preroll_item_select(interaction, self)

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="*comp cancelled.*", embed=None, view=None
        )

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True


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
        role_name = role.value if role else random.choice(ALL_ROLES)
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
        players = [player1, player2, player3, player4, player5]
        view = PreRollView(
            self, players, roll_agents, interaction.user, interaction.guild_id
        )
        embed = await view._build_status_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── Core rolling logic (called after pre-roll confirm) ────────────────────

    async def _execute_comp(
        self,
        interaction: discord.Interaction,
        players: list[discord.Member],
        roll_agents: bool,
    ):
        guild_id = interaction.guild_id

        # ── Fetch all active items ────────────────────────────────────────────
        active_items: dict[int, dict] = {}
        all_weights: dict[int, list] = {}
        all_curses: dict[int, list] = {}
        all_reductions: dict[int, list] = {}  # own role reductions
        all_curse_reds: dict[int, list] = {}  # curse-reductions aimed at others

        for player in players:
            doc = await self.bot.users_col.find_one(
                {"user_id": player.id, "guild_id": guild_id}
            )
            inv = await self.bot.inv_col.find_one(
                {"user_id": player.id, "guild_id": guild_id}
            )
            inv_items = (inv or {}).get("items", [])

            if not doc:
                continue
            if doc.get("active_comp_item"):
                active_items[player.id] = doc["active_comp_item"]
            elif not active_items.get(player.id):
                # Auto-detect reroll/swap from inventory — no pre-activation needed
                for itype in ("comp_reroll", "comp_role_swap"):
                    match = next((i for i in inv_items if i["type"] == itype), None)
                    if match:
                        active_items[player.id] = {
                            "type": itype,
                            "value": "",
                            "item_name": match.get("name", ""),
                        }
                        break
            if doc.get("active_comp_weights"):
                all_weights[player.id] = doc["active_comp_weights"]
            if doc.get("active_comp_curses"):
                all_curses[player.id] = doc["active_comp_curses"]
            if doc.get("active_comp_reductions"):
                all_reductions[player.id] = doc["active_comp_reductions"]
            if doc.get("active_comp_curse_reds"):
                all_curse_reds[player.id] = doc["active_comp_curse_reds"]

        # ── Resolve items with conflict detection ─────────────────────────────
        # Collect all lock attempts first, then resolve conflicts before finalising
        lock_attempts: dict[str, list[discord.Member]] = (
            {}
        )  # role -> [players who want it]
        agent_lock_attempts: dict[str, list[tuple[discord.Member, str]]] = (
            {}
        )  # role -> [(player, agent)]
        locked_agents: dict[int, str] = {}
        banned_roles: dict[int, str] = {}
        weighted_players: dict[int, dict[str, float]] = {}
        # cursed_players declared after curse resolution below
        swap_players: set[int] = set()
        reroll_players: set[int] = set()
        item_notes: list[str] = []
        refunded_players: list[str] = []  # notes for items returned due to conflict

        for player in players:
            item = active_items.get(player.id)
            if item:
                itype = item.get("type", "")
                value = item.get("value", "")

                if itype == "comp_role_lock" and value in ALL_ROLES:
                    lock_attempts.setdefault(value, []).append(player)

                elif itype == "comp_agent_lock":
                    agent_role = next(
                        (r for r, ag in AGENTS.items() if value in ag), None
                    )
                    if agent_role:
                        agent_lock_attempts.setdefault(agent_role, []).append(
                            (player, value)
                        )

                elif itype == "comp_role_ban" and value in ALL_ROLES:
                    banned_roles[player.id] = value
                    item_notes.append(f"🚫 {player.mention} banned **{value}**")

                elif itype == "comp_role_swap":
                    swap_players.add(player.id)
                    item_notes.append(f"🔀 {player.mention} has a **Role Swap** ready")

                elif itype == "comp_reroll":
                    reroll_players.add(player.id)
                    item_notes.append(f"🔄 {player.mention} has a **Reshuffle** ready")

            # Stacked weights (own) — build per-role delta map (+boost, -reduce), cap at 100%
            weights_list = all_weights.get(player.id, [])
            reductions_list = all_reductions.get(player.id, [])
            if weights_list or reductions_list:
                role_delta: dict[str, float] = {}
                for w in weights_list:
                    r = w.get("role") or w.get("value", "")
                    if r in ALL_ROLES:
                        role_delta[r] = role_delta.get(r, 0) + w["weight"]
                for rd in reductions_list:
                    r = rd.get("role") or rd.get("value", "")
                    if r in ALL_ROLES:
                        role_delta[r] = role_delta.get(r, 0) - rd["weight"]
                # Cap positive totals at MAX_WEIGHT
                pos_total = sum(v for v in role_delta.values() if v > 0)
                if pos_total > MAX_WEIGHT:
                    scale = MAX_WEIGHT / pos_total
                    role_delta = {
                        r: v * scale if v > 0 else v for r, v in role_delta.items()
                    }
                weighted_players[player.id] = role_delta
                parts = []
                for r, v in role_delta.items():
                    parts.append(f"**{r}** {'+'if v>=0 else ''}{int(v)}%")
                item_notes.append(f"⚖️ {player.mention}: {', '.join(parts)}")

        # Resolve role lock conflicts — one random winner per contested role, rest refunded
        locked_roles: dict[str, discord.Member] = {}
        consumed_extra: list[tuple[discord.Member, str]] = (
            []
        )  # (player, item_type) to consume normally
        refund_items: list[tuple[discord.Member, str]] = (
            []
        )  # (player, item_type) to refund

        for role, contenders in lock_attempts.items():
            # Also check if an agent lock is claiming this role
            agent_contenders = agent_lock_attempts.get(role, [])
            all_contenders = [(p, "comp_role_lock") for p in contenders] + [
                (p, "comp_agent_lock") for p, _ in agent_contenders
            ]

            if len(all_contenders) == 1:
                winner_player, winner_type = all_contenders[0]
                locked_roles[role] = winner_player
                if winner_type == "comp_agent_lock":
                    agent = next(
                        a
                        for p, a in agent_lock_attempts[role]
                        if p.id == winner_player.id
                    )
                    locked_agents[winner_player.id] = agent
                    item_notes.append(
                        f"🌟 {winner_player.mention} locked **{agent}** ({role})"
                    )
                else:
                    item_notes.append(f"🎯 {winner_player.mention} locked **{role}**")
            else:
                # Conflict — pick a winner randomly, refund the rest
                random.shuffle(all_contenders)
                winner_player, winner_type = all_contenders[0]
                locked_roles[role] = winner_player
                if winner_type == "comp_agent_lock":
                    agent = next(
                        a
                        for p, a in agent_lock_attempts[role]
                        if p.id == winner_player.id
                    )
                    locked_agents[winner_player.id] = agent
                    item_notes.append(
                        f"🌟 {winner_player.mention} locked **{agent}** ({role}) *(won tiebreak)*"
                    )
                else:
                    item_notes.append(
                        f"🎯 {winner_player.mention} locked **{role}** *(won tiebreak)*"
                    )

                for loser_player, loser_type in all_contenders[1:]:
                    refund_items.append((loser_player, loser_type))
                    refunded_players.append(
                        f"↩️ {loser_player.mention}'s **{'Role Lock' if loser_type == 'comp_role_lock' else 'Agent Lock'}** "
                        f"on **{role}** was contested — item refunded"
                    )

        # Also handle agent lock attempts for roles not contested by role locks
        for role, attempts in agent_lock_attempts.items():
            if role in locked_roles:
                continue  # already resolved above
            if len(attempts) == 1:
                player, agent = attempts[0]
                locked_roles[role] = player
                locked_agents[player.id] = agent
                item_notes.append(f"🌟 {player.mention} locked **{agent}** ({role})")
            else:
                random.shuffle(attempts)
                winner_player, winner_agent = attempts[0]
                locked_roles[role] = winner_player
                locked_agents[winner_player.id] = winner_agent
                item_notes.append(
                    f"🌟 {winner_player.mention} locked **{winner_agent}** ({role}) *(won tiebreak)*"
                )
                for loser_player, loser_agent in attempts[1:]:
                    refund_items.append((loser_player, "comp_agent_lock"))
                    refunded_players.append(
                        f"↩️ {loser_player.mention}'s **Agent Lock** on **{role}** was contested — item refunded"
                    )

        if refunded_players:
            item_notes.extend(refunded_players)

        cursed_players: dict[int, dict[str, float]] = {}  # target_id -> {role: delta}

        # Apply curses (positive) — stack additively per target per role
        for invoker_id, curses in all_curses.items():
            invoker = next((p for p in players if p.id == invoker_id), None)
            for curse in curses:
                tid = curse.get("target_id")
                target = next((p for p in players if p.id == tid), None)
                if not target:
                    continue
                c_role = curse.get("role") or curse.get("value", "")
                c_pct = int(curse.get("weight", 50))
                if c_role not in ALL_ROLES:
                    continue
                if target.id not in cursed_players:
                    cursed_players[target.id] = {}
                cursed_players[target.id][c_role] = (
                    cursed_players[target.id].get(c_role, 0) + c_pct
                )
                if invoker:
                    item_notes.append(
                        f"💀 {invoker.mention} cursed {target.mention} → **{c_role}** (+{c_pct}%)"
                    )

        # Apply curse-reductions (negative) — stack additively per target per role
        for invoker_id, creds in all_curse_reds.items():
            invoker = next((p for p in players if p.id == invoker_id), None)
            for cred in creds:
                tid = cred.get("target_id")
                target = next((p for p in players if p.id == tid), None)
                if not target:
                    continue
                c_role = cred.get("role") or cred.get("value", "")
                c_pct = int(cred.get("weight", 30))
                if c_role not in ALL_ROLES:
                    continue
                if target.id not in cursed_players:
                    cursed_players[target.id] = {}
                cursed_players[target.id][c_role] = (
                    cursed_players[target.id].get(c_role, 0) - c_pct
                )
                if invoker:
                    item_notes.append(
                        f"🪄 {invoker.mention} reduced {target.mention}'s **{c_role}** (-{c_pct}%)"
                    )

        # Cap positive curse totals at MAX_WEIGHT per target
        for tid, cmap in cursed_players.items():
            pos_total = sum(v for v in cmap.values() if v > 0)
            if pos_total > MAX_WEIGHT:
                scale = MAX_WEIGHT / pos_total
                cursed_players[tid] = {
                    r: v * scale if v > 0 else v for r, v in cmap.items()
                }

        # ── Assign roles ──────────────────────────────────────────────────────
        unassigned = [
            p for p in players if p.id not in {m.id for m in locked_roles.values()}
        ]
        remaining = [r for r in ALL_ROLES if r not in locked_roles]
        random.shuffle(unassigned)
        random.shuffle(remaining)

        assignments: dict[str, discord.Member] = dict(locked_roles)
        taken: set[str] = set(locked_roles.keys())

        # Biased = anyone with a weight/reduction/curse/curse-reduction
        biased = [
            p for p in unassigned if p.id in weighted_players or p.id in cursed_players
        ]
        unbiased = [p for p in unassigned if p not in biased]

        for player in biased:
            avail = [
                r
                for r in remaining
                if r not in taken and r != banned_roles.get(player.id)
            ]
            if not avail:
                avail = [r for r in remaining if r not in taken]
            if not avail:
                continue

            if player.id in cursed_players:
                role_map = cursed_players[player.id]
                chosen = _multi_weighted_choice(avail, role_map)
            else:
                role_map = weighted_players[player.id]
                # Check if any preferred role is still available
                preferred_available = [r for r in role_map if r in avail]
                chosen = _multi_weighted_choice(avail, role_map)
                if not preferred_available:
                    item_notes.append(
                        f"⚖️ {player.mention}'s weight couldn't apply — all preferred roles already taken"
                    )

            assignments[chosen] = player
            taken.add(chosen)

        # Unweighted — greedy by fewest eligible
        eligible_list = []
        for player in unbiased:
            banned = banned_roles.get(player.id)
            elig = [r for r in remaining if r not in taken and r != banned]
            eligible_list.append((player, elig))
        eligible_list.sort(key=lambda x: len(x[1]))

        for player, elig in eligible_list:
            avail = [r for r in elig if r not in taken]
            if not avail:
                avail = [r for r in remaining if r not in taken]
            if not avail:
                continue
            chosen = random.choice(avail)
            assignments[chosen] = player
            taken.add(chosen)

        free_pick = next(
            (p for p in players if p.id not in {m.id for m in assignments.values()}),
            players[4],
        )
        assignments["Free Pick"] = free_pick

        # ── Roll agents ───────────────────────────────────────────────────────
        rolled_agents: dict[str, str] = {}
        if roll_agents:
            locked_agent_names = set(locked_agents.values())
            pool = [a for a in ALL_AGENTS if a not in locked_agent_names]
            random.shuffle(pool)
            for role in ALL_ROLES:
                player = assignments.get(role)
                if player and player.id in locked_agents:
                    rolled_agents[role] = locked_agents[player.id]
                else:
                    role_pool = [a for a in pool if a in AGENTS[role]]
                    if role_pool:
                        chosen_agent = role_pool[0]
                        pool.remove(chosen_agent)
                    elif pool:
                        chosen_agent = pool.pop(0)
                    else:
                        chosen_agent = random.choice(AGENTS[role])
                    rolled_agents[role] = chosen_agent
            rolled_agents["Free Pick"] = (
                pool.pop(0) if pool else random.choice(ALL_AGENTS)
            )

        # ── Consume items ─────────────────────────────────────────────────────
        # refund_items: (player, type) pairs where lock was contested — keep item in inventory
        refund_set: set[int] = {p.id for p, _ in refund_items}

        for player in players:
            unsets: dict = {}
            pulls: list = []

            item = active_items.get(player.id)
            if item and item.get("type") not in ("comp_role_swap", "comp_reroll"):
                # Locks/bans/weights consumed immediately
                if player.id in refund_set:
                    unsets["active_comp_item"] = ""
                else:
                    pulls.append({"type": item["type"]})
                    unsets["active_comp_item"] = ""
            elif item and item.get("type") == "comp_reroll":
                # Reroll consumed only when button is clicked (handled in CompPostView)
                # Just clear the active queue field if it was manually queued
                if doc := await self.bot.users_col.find_one(
                    {"user_id": player.id, "guild_id": guild_id}
                ):
                    if doc.get("active_comp_item", {}).get("type") == "comp_reroll":
                        unsets["active_comp_item"] = ""

            if player.id in all_weights:
                for w in all_weights[player.id]:
                    pulls.append(
                        {"type": "comp_weight", "name": w.get("item_name", "")}
                    )
                unsets["active_comp_weights"] = ""

            if player.id in all_curses:
                for c in all_curses[player.id]:
                    pulls.append({"type": "comp_curse", "name": c.get("item_name", "")})
                unsets["active_comp_curses"] = ""

            if player.id in all_reductions:
                for rd in all_reductions[player.id]:
                    pulls.append(
                        {"type": "comp_reduce", "name": rd.get("item_name", "")}
                    )
                unsets["active_comp_reductions"] = ""

            if player.id in all_curse_reds:
                for cr in all_curse_reds[player.id]:
                    pulls.append(
                        {"type": "comp_curse_reduce", "name": cr.get("item_name", "")}
                    )
                unsets["active_comp_curse_reds"] = ""

            if pulls:
                for pull in pulls:
                    pull_q = {k: v for k, v in pull.items() if v}
                    await self.bot.inv_col.update_one(
                        {"user_id": player.id, "guild_id": guild_id},
                        {"$pull": {"items": pull_q}},
                    )
            if unsets:
                await self.bot.users_col.update_one(
                    {"user_id": player.id, "guild_id": guild_id},
                    {"$unset": unsets},
                )

        # ── Post result ───────────────────────────────────────────────────────
        locked_player_ids = {m.id for m in locked_roles.values()}
        embed = _build_comp_embed(
            assignments,
            rolled_agents,
            item_notes,
            swap_players,
            reroll_players,
            interaction.guild.name,
        )
        needs_view = swap_players or reroll_players
        view = (
            CompPostView(
                assignments,
                swap_players,
                reroll_players,
                locked_player_ids,
                guild_id,
                self.bot,
                roll_agents,
                rolled_agents,
            )
            if needs_view
            else None
        )
        pings = " ".join(p.mention for p in players)
        if view:
            await interaction.followup.send(content=pings, embed=embed, view=view)
        else:
            await interaction.followup.send(content=pings, embed=embed)

        # ── Record for weekly recap ───────────────────────────────────────────
        now = datetime.now(timezone.utc)
        week = (now - timedelta(days=(now.weekday() + 1) % 7)).strftime("%Y-%m-%d")
        for role, member in assignments.items():
            await self.bot.comp_rolls_col.update_one(
                {
                    "guild_id": guild_id,
                    "user_id": member.id,
                    "week": week,
                    "role": role,
                },
                {"$inc": {"count": 1}},
                upsert=True,
            )
            await self.bot.users_col.update_one(
                {"guild_id": guild_id, "user_id": member.id},
                {"$inc": {f"comp_roles.{role}": 1}},
                upsert=True,
            )

    # ── Pre-roll item select (shown from the pre-roll dialogue) ───────────────

    async def _show_preroll_item_select(
        self, interaction: discord.Interaction, pre_roll_view: PreRollView
    ):
        """Show the item activation menu. Back button returns to pre-roll screen."""
        COMP_TIME_ONLY = {
            "comp_reroll",
            "comp_role_swap",
            "comp_curse",
            "comp_curse_reduce",
        }
        inv = await self.bot.inv_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        items = [
            i for i in (inv.get("items", []) if inv else []) if i["type"] in COMP_TYPES
        ]
        activatable = [i for i in items if i["type"] not in COMP_TIME_ONLY]
        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )

        if not activatable:
            # Only comp-time items in inventory — nothing to activate manually
            comp_time = [i for i in items if i["type"] in COMP_TIME_ONLY]
            if comp_time:
                embed = await pre_roll_view._build_status_embed()
                await interaction.response.edit_message(
                    content="*your items (🔄 🔀 💀) activate automatically at roll time — nothing to configure here.*",
                    embed=embed,
                    view=pre_roll_view,
                )
            else:
                embed = await pre_roll_view._build_status_embed()
                await interaction.response.edit_message(
                    content="*no comp items in your inventory.*",
                    embed=embed,
                    view=pre_roll_view,
                )
            return

        # Count stackable items
        weight_count = sum(1 for i in activatable if i["type"] == "comp_weight")
        curse_count = sum(1 for i in activatable if i["type"] == "comp_curse")
        cur_weights = (user_doc or {}).get("active_comp_weights", [])
        cur_total_w = (
            min(sum(w["weight"] for w in cur_weights), MAX_WEIGHT) if cur_weights else 0
        )

        seen: set[str] = set()
        options = []
        for item in activatable:
            key = item["type"]
            if key in seen:
                continue
            seen.add(key)
            label = LABEL_MAP.get(key, key)
            if key == "comp_weight":
                label += f" (×{weight_count}, {cur_total_w}% stacked)"
            elif key == "comp_curse":
                label += f" (×{curse_count})"
            options.append(
                discord.SelectOption(
                    label=label, value=key, description=item.get("name", "")
                )
            )

        select = discord.ui.Select(
            placeholder="Choose an item to activate...", options=options
        )

        async def on_select(sel: discord.Interaction):
            chosen_type = select.values[0]
            await self._activate_item_flow(sel, chosen_type, activatable, pre_roll_view)

        select.callback = on_select

        back_btn = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary, row=1
        )

        async def on_back(btn: discord.Interaction):
            embed = await pre_roll_view._build_status_embed()
            await btn.response.edit_message(embed=embed, view=pre_roll_view)

        back_btn.callback = on_back

        v = discord.ui.View(timeout=90)
        v.add_item(select)
        v.add_item(back_btn)
        await interaction.response.edit_message(
            content="*choose an item to activate:*", embed=None, view=v
        )

    async def _activate_item_flow(
        self,
        interaction: discord.Interaction,
        chosen_type: str,
        items: list,
        pre_roll_view: PreRollView,
    ):
        """Handle activation of each item type, returning to pre-roll after."""
        back_label = "◀ Back to items"

        async def back_to_items(btn: discord.Interaction):
            await self._show_preroll_item_select(btn, pre_roll_view)

        async def back_to_preroll(btn: discord.Interaction):
            embed = await pre_roll_view._build_status_embed()
            await btn.response.edit_message(
                content=None, embed=embed, view=pre_roll_view
            )

        def _back_btn(row=1):
            b = discord.ui.Button(
                label=back_label, style=discord.ButtonStyle.secondary, row=row
            )
            b.callback = back_to_items
            return b

        if chosen_type in ("comp_reroll", "comp_role_swap"):
            await self.bot.users_col.update_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
                {"$set": {"active_comp_item": {"type": chosen_type, "value": ""}}},
                upsert=True,
            )
            label = LABEL_MAP[chosen_type]
            if chosen_type == "comp_reroll":
                hint = "After the comp posts, a **🔄 Reshuffle** button will appear — click it to randomly redistribute all unlocked roles."
            else:
                hint = "After the comp posts, a **🔀 Swap** button will appear for you."
            embed = await pre_roll_view._build_status_embed()
            await interaction.response.edit_message(
                content=None, embed=embed, view=pre_roll_view
            )
            return

        if chosen_type in ("comp_role_lock", "comp_role_ban"):
            rs = discord.ui.Select(
                placeholder="Choose a role...",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_role(sel: discord.Interaction):
                chosen_role = rs.values[0]
                await self.bot.users_col.update_one(
                    {"user_id": sel.user.id, "guild_id": sel.guild_id},
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
                embed = await pre_roll_view._build_status_embed()
                await sel.response.edit_message(
                    content=None, embed=embed, view=pre_roll_view
                )

            rs.callback = on_role
            v = discord.ui.View(timeout=60)
            v.add_item(rs)
            v.add_item(_back_btn())
            prompt = (
                "🎯 Lock which role?"
                if chosen_type == "comp_role_lock"
                else "🚫 Ban which role?"
            )
            await interaction.response.edit_message(content=prompt, embed=None, view=v)
            return

        if chosen_type == "comp_agent_lock":
            rs = discord.ui.Select(
                placeholder="First, choose a role...",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_role_for_agent(sel: discord.Interaction):
                role = rs.values[0]
                ag_select = discord.ui.Select(
                    placeholder=f"Choose a {role} agent...",
                    options=[
                        discord.SelectOption(label=a, value=a) for a in AGENTS[role]
                    ],
                )

                async def on_agent(as_: discord.Interaction):
                    chosen_agent = ag_select.values[0]
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
                    embed = await pre_roll_view._build_status_embed()
                    await as_.response.edit_message(
                        content=None, embed=embed, view=pre_roll_view
                    )

                ag_select.callback = on_agent
                v2 = discord.ui.View(timeout=60)
                v2.add_item(ag_select)
                v2.add_item(_back_btn())
                await sel.response.edit_message(
                    content=f"🌟 Choose a **{role}** agent:", embed=None, view=v2
                )

            rs.callback = on_role_for_agent
            v = discord.ui.View(timeout=60)
            v.add_item(rs)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content="🌟 Choose the role first:", embed=None, view=v
            )
            return

        if chosen_type == "comp_weight":
            inv_item = next((i for i in items if i["type"] == "comp_weight"), None)
            shop_item = await self.bot.items_col.find_one(
                {"guild_id": interaction.guild_id, "name": inv_item["name"]}
                if inv_item
                else {"guild_id": interaction.guild_id, "type": "comp_weight"}
            )
            w_pct = int((shop_item or {}).get("weight_pct", 10))
            item_name = inv_item["name"] if inv_item else ""
            owned_count = sum(1 for i in items if i["type"] == "comp_weight")

            user_doc = await self.bot.users_col.find_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
            )
            cur = (user_doc or {}).get("active_comp_weights", [])
            cur_total = sum(w["weight"] for w in cur)

            if cur_total >= MAX_WEIGHT:
                embed = await pre_roll_view._build_status_embed()
                await interaction.response.edit_message(
                    content=f"⚖️ Already at max weight ({cur_total}% / {MAX_WEIGHT}%).",
                    embed=embed,
                    view=pre_roll_view,
                )
                return

            # Step 1: pick which role to weight toward
            role_select = discord.ui.Select(
                placeholder="Which role do you want to weight toward?",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_weight_role(sel: discord.Interaction):
                chosen_role = role_select.values[0]
                # How much is already allocated to this specific role?
                role_cur = sum(
                    w["weight"]
                    for w in cur
                    if (w.get("role") or w.get("value", "")) == chosen_role
                )
                remaining_cap = MAX_WEIGHT - cur_total
                max_usable = min(owned_count, remaining_cap // w_pct if w_pct else 1)

                if max_usable <= 0:
                    embed = await pre_roll_view._build_status_embed()
                    await sel.response.edit_message(
                        content=f"⚖️ Total weight pool is full ({cur_total}% / {MAX_WEIGHT}%).",
                        embed=embed,
                        view=pre_roll_view,
                    )
                    return

                # Step 2: pick quantity
                qty_options = [
                    discord.SelectOption(
                        label=f"×{n}  →  +{n * w_pct}% {chosen_role}  (pool total: {min(cur_total + n * w_pct, MAX_WEIGHT)}%)",
                        value=str(n),
                    )
                    for n in range(1, max_usable + 1)
                ]
                qty_select = discord.ui.Select(
                    placeholder=f"How many? (own {owned_count}, each +{w_pct}%  •  {remaining_cap}% pool remaining)",
                    options=qty_options,
                )

                async def on_qty(as_: discord.Interaction):
                    n = int(qty_select.values[0])
                    for _ in range(n):
                        await self.bot.users_col.update_one(
                            {"user_id": as_.user.id, "guild_id": as_.guild_id},
                            {
                                "$push": {
                                    "active_comp_weights": {
                                        "role": chosen_role,
                                        "weight": w_pct,
                                        "item_name": item_name,
                                    }
                                }
                            },
                            upsert=True,
                        )
                    new_total = min(cur_total + n * w_pct, MAX_WEIGHT)
                    embed = await pre_roll_view._build_status_embed()
                    await as_.response.edit_message(
                        content=f"⚖️ Stacked ×{n}! **{chosen_role}** +{n * w_pct}% (pool now {new_total}% / {MAX_WEIGHT}%).",
                        embed=embed,
                        view=pre_roll_view,
                    )

                qty_select.callback = on_qty
                v2 = discord.ui.View(timeout=60)
                v2.add_item(qty_select)
                v2.add_item(_back_btn())
                cur_breakdown = (
                    ", ".join(
                        f"{r}: {sum(w['weight'] for w in cur if (w.get('role') or w.get('value','')) == r)}%"
                        for r in ALL_ROLES
                        if any((w.get("role") or w.get("value", "")) == r for w in cur)
                    )
                    or "none"
                )
                await sel.response.edit_message(
                    content=f"⚖️ Adding weight toward **{chosen_role}** (+{w_pct}% each, {remaining_cap}% pool left).\nCurrent weights: {cur_breakdown}\nHow many?",
                    embed=None,
                    view=v2,
                )

            role_select.callback = on_weight_role
            v = discord.ui.View(timeout=60)
            v.add_item(role_select)
            v.add_item(_back_btn())
            cur_breakdown = (
                ", ".join(
                    f"{r}: {sum(w['weight'] for w in cur if (w.get('role') or w.get('value','')) == r)}%"
                    for r in ALL_ROLES
                    if any((w.get("role") or w.get("value", "")) == r for w in cur)
                )
                or "none"
            )
            await interaction.response.edit_message(
                content=f"⚖️ Each **{item_name}** adds **+{w_pct}%** to any role (pool: {cur_total}% / {MAX_WEIGHT}% used).\nCurrent: {cur_breakdown}\nWhich role?",
                embed=None,
                view=v,
            )
            return

        if chosen_type == "comp_curse":
            inv_item = next((i for i in items if i["type"] == "comp_curse"), None)
            shop_item = await self.bot.items_col.find_one(
                {"guild_id": interaction.guild_id, "name": inv_item["name"]}
                if inv_item
                else {"guild_id": interaction.guild_id, "type": "comp_curse"}
            )
            c_pct = int((shop_item or {}).get("curse_pct", 30))
            item_name = inv_item["name"] if inv_item else ""
            owned_count = sum(1 for i in items if i["type"] == "comp_curse")

            # Step 1: pick target
            target_opts = [
                discord.SelectOption(label=p.display_name, value=str(p.id))
                for p in pre_roll_view.players
                if p.id != interaction.user.id
            ]
            if not target_opts:
                await interaction.response.send_message(
                    "*no other players to curse.*", ephemeral=True
                )
                return

            target_select = discord.ui.Select(
                placeholder="Who do you want to curse?", options=target_opts
            )

            async def on_curse_target(sel: discord.Interaction):
                target_id = int(target_select.values[0])
                target_member = discord.utils.get(pre_roll_view.players, id=target_id)

                # Fetch current curses aimed at this target
                user_doc2 = await self.bot.users_col.find_one(
                    {"user_id": sel.user.id, "guild_id": sel.guild_id}
                )
                cur_curses = [
                    c
                    for c in (user_doc2 or {}).get("active_comp_curses", [])
                    if c.get("target_id") == target_id
                ]
                cur_total = sum(c["weight"] for c in cur_curses)

                if cur_total >= MAX_WEIGHT:
                    await sel.response.edit_message(
                        content=f"💀 Already at 100% curse on **{target_member.display_name}**.",
                        embed=None,
                        view=None,
                    )
                    return

                # Step 2: pick role
                role_select = discord.ui.Select(
                    placeholder="Which role to push them toward?",
                    options=[
                        discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                        for r in ALL_ROLES
                    ],
                )

                async def on_curse_role(rs: discord.Interaction):
                    chosen_role = role_select.values[0]
                    remaining_cap = MAX_WEIGHT - cur_total
                    max_usable = min(
                        owned_count, remaining_cap // c_pct if c_pct else 1
                    )

                    if max_usable <= 0:
                        embed = await pre_roll_view._build_status_embed()
                        await rs.response.edit_message(
                            content=f"💀 Curse pool for **{target_member.display_name}** is full ({cur_total}% / {MAX_WEIGHT}%).",
                            embed=embed,
                            view=pre_roll_view,
                        )
                        return

                    # Step 3: pick quantity
                    qty_options = [
                        discord.SelectOption(
                            label=f"×{n}  →  +{n * c_pct}% {chosen_role}  (total: {min(cur_total + n * c_pct, MAX_WEIGHT)}%)",
                            value=str(n),
                        )
                        for n in range(1, max_usable + 1)
                    ]
                    qty_select = discord.ui.Select(
                        placeholder=f"How many? ({remaining_cap}% remaining, each +{c_pct}%)",
                        options=qty_options,
                    )

                    async def on_curse_qty(as_: discord.Interaction):
                        n = int(qty_select.values[0])
                        for _ in range(n):
                            await self.bot.users_col.update_one(
                                {"user_id": as_.user.id, "guild_id": as_.guild_id},
                                {
                                    "$push": {
                                        "active_comp_curses": {
                                            "target_id": target_id,
                                            "role": chosen_role,
                                            "weight": c_pct,
                                            "item_name": item_name,
                                        }
                                    }
                                },
                                upsert=True,
                            )
                        new_total = min(cur_total + n * c_pct, MAX_WEIGHT)
                        embed = await pre_roll_view._build_status_embed()
                        await as_.response.edit_message(
                            content=f"💀 Cursed ×{n}! **{target_member.display_name}** pushed toward **{chosen_role}** ({new_total}% total).",
                            embed=embed,
                            view=pre_roll_view,
                        )

                    qty_select.callback = on_curse_qty
                    v3 = discord.ui.View(timeout=60)
                    v3.add_item(qty_select)
                    v3.add_item(_back_btn())
                    cur_breakdown = (
                        ", ".join(
                            f"{c.get('role','?')}: {c['weight']}%" for c in cur_curses
                        )
                        or "none"
                    )
                    await rs.response.edit_message(
                        content=f"💀 Cursing **{target_member.display_name}** toward **{chosen_role}** (+{c_pct}% each, {remaining_cap}% left).\nCurrent curses on them: {cur_breakdown}\nHow many?",
                        embed=None,
                        view=v3,
                    )

                role_select.callback = on_curse_role
                v2 = discord.ui.View(timeout=60)
                v2.add_item(role_select)
                v2.add_item(_back_btn())
                cur_breakdown = (
                    ", ".join(
                        f"{c.get('role','?')}: {c['weight']}%" for c in cur_curses
                    )
                    or "none"
                )
                await sel.response.edit_message(
                    content=f"💀 Cursing **{target_member.display_name}** (current: {cur_total}% / {MAX_WEIGHT}%  •  {cur_breakdown})\nWhich role to push them toward?",
                    embed=None,
                    view=v2,
                )

            target_select.callback = on_curse_target
            v = discord.ui.View(timeout=60)
            v.add_item(target_select)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content=f"💀 Each **{item_name}** adds **+{c_pct}%** toward any role (player picks). Choose your target:",
                embed=None,
                view=v,
            )
            return

        if chosen_type == "comp_reduce":
            inv_item = next((i for i in items if i["type"] == "comp_reduce"), None)
            shop_item = await self.bot.items_col.find_one(
                {"guild_id": interaction.guild_id, "name": inv_item["name"]}
                if inv_item
                else {"guild_id": interaction.guild_id, "type": "comp_reduce"}
            )
            r_pct = int((shop_item or {}).get("reduce_pct", 20))
            item_name = inv_item["name"] if inv_item else ""
            owned_count = sum(1 for i in items if i["type"] == "comp_reduce")

            user_doc = await self.bot.users_col.find_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
            )
            cur_reds = (user_doc or {}).get("active_comp_reductions", [])
            cur_total = sum(w["weight"] for w in cur_reds)

            role_select = discord.ui.Select(
                placeholder="Which role do you want to reduce?",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_reduce_role(sel: discord.Interaction):
                chosen_role = role_select.values[0]
                role_cur = sum(
                    w["weight"]
                    for w in cur_reds
                    if (w.get("role") or w.get("value", "")) == chosen_role
                )
                max_usable = min(owned_count, (100 - role_cur) // r_pct if r_pct else 1)

                if max_usable <= 0:
                    embed = await pre_roll_view._build_status_embed()
                    await sel.response.edit_message(
                        content=f"⬇️ **{chosen_role}** is already fully reduced.",
                        embed=embed,
                        view=pre_roll_view,
                    )
                    return

                qty_options = [
                    discord.SelectOption(
                        label=f"×{n}  →  -{n * r_pct}% {chosen_role}  (role total: {min(role_cur + n * r_pct, 100)}%)",
                        value=str(n),
                    )
                    for n in range(1, max_usable + 1)
                ]
                qty_select = discord.ui.Select(
                    placeholder=f"How many? (each -{r_pct}%)", options=qty_options
                )

                async def on_reduce_qty(as_: discord.Interaction):
                    n = int(qty_select.values[0])
                    for _ in range(n):
                        await self.bot.users_col.update_one(
                            {"user_id": as_.user.id, "guild_id": as_.guild_id},
                            {
                                "$push": {
                                    "active_comp_reductions": {
                                        "role": chosen_role,
                                        "weight": r_pct,
                                        "item_name": item_name,
                                    }
                                }
                            },
                            upsert=True,
                        )
                    embed = await pre_roll_view._build_status_embed()
                    await as_.response.edit_message(
                        content=f"⬇️ Reduced ×{n}! **{chosen_role}** -{n * r_pct}% probability.",
                        embed=embed,
                        view=pre_roll_view,
                    )

                qty_select.callback = on_reduce_qty
                v2 = discord.ui.View(timeout=60)
                v2.add_item(qty_select)
                v2.add_item(_back_btn())
                await sel.response.edit_message(
                    content=f"⬇️ How many reductions on **{chosen_role}**?",
                    embed=None,
                    view=v2,
                )

            role_select.callback = on_reduce_role
            v = discord.ui.View(timeout=60)
            v.add_item(role_select)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content=f"⬇️ Each **{item_name}** reduces a role by **{r_pct}%**. Which role?",
                embed=None,
                view=v,
            )
            return

        if chosen_type == "comp_curse_reduce":
            inv_item = next(
                (i for i in items if i["type"] == "comp_curse_reduce"), None
            )
            shop_item = await self.bot.items_col.find_one(
                {"guild_id": interaction.guild_id, "name": inv_item["name"]}
                if inv_item
                else {"guild_id": interaction.guild_id, "type": "comp_curse_reduce"}
            )
            cr_pct = int((shop_item or {}).get("curse_reduce_pct", 20))
            item_name = inv_item["name"] if inv_item else ""
            owned_count = sum(1 for i in items if i["type"] == "comp_curse_reduce")

            target_opts = [
                discord.SelectOption(label=p.display_name, value=str(p.id))
                for p in pre_roll_view.players
                if p.id != interaction.user.id
            ]
            if not target_opts:
                await interaction.response.send_message(
                    "*no other players to curse.*", ephemeral=True
                )
                return

            target_select = discord.ui.Select(
                placeholder="Who do you want to reduce?", options=target_opts
            )

            async def on_cr_target(sel: discord.Interaction):
                target_id = int(target_select.values[0])
                target_member = discord.utils.get(pre_roll_view.players, id=target_id)
                user_doc2 = await self.bot.users_col.find_one(
                    {"user_id": sel.user.id, "guild_id": sel.guild_id}
                )
                cur_creds = [
                    c
                    for c in (user_doc2 or {}).get("active_comp_curse_reds", [])
                    if c.get("target_id") == target_id
                ]

                role_select = discord.ui.Select(
                    placeholder="Which role to reduce for them?",
                    options=[
                        discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                        for r in ALL_ROLES
                    ],
                )

                async def on_cr_role(rs: discord.Interaction):
                    chosen_role = role_select.values[0]
                    role_cur = sum(
                        c["weight"]
                        for c in cur_creds
                        if (c.get("role") or c.get("value", "")) == chosen_role
                    )
                    max_usable = min(
                        owned_count, (100 - role_cur) // cr_pct if cr_pct else 1
                    )

                    if max_usable <= 0:
                        embed = await pre_roll_view._build_status_embed()
                        await rs.response.edit_message(
                            content=f"🪄 **{chosen_role}** is already fully reduced for {target_member.display_name}.",
                            embed=embed,
                            view=pre_roll_view,
                        )
                        return

                    qty_options = [
                        discord.SelectOption(
                            label=f"×{n}  →  -{n * cr_pct}% {chosen_role} for {target_member.display_name}",
                            value=str(n),
                        )
                        for n in range(1, max_usable + 1)
                    ]
                    qty_select = discord.ui.Select(
                        placeholder=f"How many? (each -{cr_pct}%)", options=qty_options
                    )

                    async def on_cr_qty(as_: discord.Interaction):
                        n = int(qty_select.values[0])
                        for _ in range(n):
                            await self.bot.users_col.update_one(
                                {"user_id": as_.user.id, "guild_id": as_.guild_id},
                                {
                                    "$push": {
                                        "active_comp_curse_reds": {
                                            "target_id": target_id,
                                            "role": chosen_role,
                                            "weight": cr_pct,
                                            "item_name": item_name,
                                        }
                                    }
                                },
                                upsert=True,
                            )
                        embed = await pre_roll_view._build_status_embed()
                        await as_.response.edit_message(
                            content=f"🪄 Reduced ×{n}! **{target_member.display_name}**'s **{chosen_role}** -{n * cr_pct}%.",
                            embed=embed,
                            view=pre_roll_view,
                        )

                    qty_select.callback = on_cr_qty
                    v3 = discord.ui.View(timeout=60)
                    v3.add_item(qty_select)
                    v3.add_item(_back_btn())
                    await rs.response.edit_message(
                        content=f"🪄 How many for **{target_member.display_name}**'s **{chosen_role}**?",
                        embed=None,
                        view=v3,
                    )

                role_select.callback = on_cr_role
                v2 = discord.ui.View(timeout=60)
                v2.add_item(role_select)
                v2.add_item(_back_btn())
                await sel.response.edit_message(
                    content=f"🪄 Which role to reduce for **{target_member.display_name}**?",
                    embed=None,
                    view=v2,
                )

            target_select.callback = on_cr_target
            v = discord.ui.View(timeout=60)
            v.add_item(target_select)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content=f"🪄 Each **{item_name}** reduces a role by **{cr_pct}%** for a chosen player. Target:",
                embed=None,
                view=v,
            )
            return

    # ── /useitem (standalone — for non-rollers) ───────────────────────────────

    @app_commands.command(
        name="useitem",
        description="Queue a comp item before the next /randomcomp you're in",
    )
    async def useitem(self, interaction: discord.Interaction):
        """
        Full activation flow for players who aren't the one running /randomcomp.
        Everything except comp_curse (which needs to know the comp players) works here.
        """
        await self._standalone_item_menu(interaction)

    async def _standalone_item_menu(self, interaction: discord.Interaction):
        """Show item status + activate button. Usable standalone or as a re-entry point."""
        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        d = user_doc or {}
        active = d.get("active_comp_item")
        weights = d.get("active_comp_weights", [])
        curses = d.get("active_comp_curses", [])

        inv = await self.bot.inv_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        items = [
            i for i in (inv.get("items", []) if inv else []) if i["type"] in COMP_TYPES
        ]

        if not items and not active and not weights and not curses:
            await interaction.response.send_message(
                "*you don't have any comp items.* Pick some up in the shop! 🛒",
                ephemeral=True,
            )
            return

        # Build status
        queued_lines = []
        if active:
            label = LABEL_MAP.get(active.get("type", ""), "?")
            val = active.get("value", "")
            queued_lines.append(f"• {label}" + (f": **{val}**" if val else ""))
        if weights:
            role_totals: dict[str, int] = {}
            for w in weights:
                r = w.get("role") or w.get("value", "?")
                role_totals[r] = role_totals.get(r, 0) + w["weight"]
            pool_total = min(sum(role_totals.values()), MAX_WEIGHT)
            breakdown = ", ".join(f"**{r}** +{v}%" for r, v in role_totals.items())
            queued_lines.append(f"• ⚖️ Weight: {breakdown}  ({pool_total}% pool)")
        if curses:
            queued_lines.append(
                f"• 💀 {len(curses)} curse(s) queued *(target set at comp time)*"
            )

        COMP_TIME_ONLY = {
            "comp_curse",
            "comp_curse_reduce",
            "comp_reroll",
            "comp_role_swap",
        }
        activatable = [i for i in items if i["type"] not in COMP_TIME_ONLY]
        comp_time = [i for i in items if i["type"] in COMP_TIME_ONLY]

        status = "\n".join(queued_lines) if queued_lines else "*nothing queued yet*"

        inv_note = f"**In inventory:** {len(activatable)} activatable item(s)"
        if comp_time:
            type_counts = {}
            for i in comp_time:
                type_counts[i["type"]] = type_counts.get(i["type"], 0) + 1
            ct_parts = ", ".join(
                f"{LABEL_MAP.get(t, t)} ×{n}" for t, n in type_counts.items()
            )
            inv_note += f"\n*(comp-time only: {ct_parts} — activates automatically when you're in a comp)*"

        embed = discord.Embed(
            title="🎒 Comp Items",
            description=f"**Queued for next `/randomcomp`:**\n{status}\n\n{inv_note}",
            color=COLOUR_MAIN,
        )
        embed.set_footer(
            text="Items activate automatically when /randomcomp is rolled while you're in it."
        )

        view = discord.ui.View(timeout=90)

        if activatable:
            act_btn = discord.ui.Button(
                label="🎒 Activate an item", style=discord.ButtonStyle.primary
            )

            async def on_activate(btn: discord.Interaction):
                await self._standalone_activate_flow(btn)

            act_btn.callback = on_activate
            view.add_item(act_btn)

        if active or weights or curses:
            clr_btn = discord.ui.Button(
                label="❌ Clear all queued", style=discord.ButtonStyle.danger
            )

            async def on_clear(btn: discord.Interaction):
                await self.bot.users_col.update_one(
                    {"user_id": btn.user.id, "guild_id": btn.guild_id},
                    {
                        "$unset": {
                            "active_comp_item": "",
                            "active_comp_weights": "",
                            "active_comp_curses": "",
                            "active_comp_reductions": "",
                            "active_comp_curse_reds": "",
                        }
                    },
                )
                await btn.response.edit_message(
                    content="❌ All queued items cleared — they're still in your inventory.",
                    embed=None,
                    view=None,
                )

            clr_btn.callback = on_clear
            view.add_item(clr_btn)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )

    async def _standalone_activate_flow(self, interaction: discord.Interaction):
        """Item activation for non-rollers. Same as pre-roll but no curse target picker."""
        inv = await self.bot.inv_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        COMP_TIME_ONLY = {
            "comp_curse",
            "comp_curse_reduce",
            "comp_reroll",
            "comp_role_swap",
        }
        items = [
            i
            for i in (inv.get("items", []) if inv else [])
            if i["type"] in COMP_TYPES and i["type"] not in COMP_TIME_ONLY
        ]

        if not items:
            await interaction.response.send_message(
                "*no activatable items here.*\n"
                "🔄 Reroll and 🔀 Swap activate after the comp posts.\n"
                "💀 Curses activate from the roller's pre-roll screen.",
                ephemeral=True,
            )
            return

        seen: set[str] = set()
        options = []
        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        cur_weights = (user_doc or {}).get("active_comp_weights", [])
        cur_total_w = (
            min(sum(w["weight"] for w in cur_weights), MAX_WEIGHT) if cur_weights else 0
        )

        for item in items:
            key = item["type"]
            if key in seen:
                continue
            seen.add(key)
            label = LABEL_MAP.get(key, key)
            count = sum(1 for i in items if i["type"] == key)
            if key == "comp_weight":
                label += f" (×{count}, {cur_total_w}% stacked so far)"
            elif count > 1:
                label += f" (×{count})"
            options.append(
                discord.SelectOption(
                    label=label, value=key, description=item.get("name", "")
                )
            )

        select = discord.ui.Select(
            placeholder="Choose an item to activate...", options=options
        )

        async def on_select(sel: discord.Interaction):
            chosen_type = select.values[0]
            await self._standalone_type_handler(sel, chosen_type, items)

        select.callback = on_select

        back_btn = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary, row=1
        )

        async def on_back(btn: discord.Interaction):
            await self._standalone_item_menu(btn)

        back_btn.callback = on_back

        v = discord.ui.View(timeout=90)
        v.add_item(select)
        v.add_item(back_btn)

        if interaction.response.is_done():
            await interaction.edit_original_response(
                content="*choose an item to activate:*", embed=None, view=v
            )
        else:
            await interaction.response.edit_message(
                content="*choose an item to activate:*", embed=None, view=v
            )

    async def _standalone_type_handler(
        self, interaction: discord.Interaction, chosen_type: str, items: list
    ):
        """Handle each item type activation for standalone /useitem."""

        async def back_to_menu(i: discord.Interaction):
            await self._standalone_item_menu(i)

        async def back_to_activate(i: discord.Interaction):
            await self._standalone_activate_flow(i)

        def _back_btn(row=1):
            b = discord.ui.Button(
                label="◀ Back", style=discord.ButtonStyle.secondary, row=row
            )
            b.callback = back_to_activate
            return b

        if chosen_type in ("comp_reroll", "comp_role_swap"):
            await self.bot.users_col.update_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
                {"$set": {"active_comp_item": {"type": chosen_type, "value": ""}}},
                upsert=True,
            )
            label = LABEL_MAP[chosen_type]
            if chosen_type == "comp_reroll":
                hint = "After the comp posts, click **🔄 Reshuffle** to randomly redistribute all unlocked roles."
            else:
                hint = "After the comp posts, click **🔀 Swap** to swap roles with another player."
            await interaction.response.edit_message(
                content=f"✅ **{label}** queued! {hint}",
                embed=None,
                view=None,
            )
            return

        if chosen_type in ("comp_role_lock", "comp_role_ban"):
            rs = discord.ui.Select(
                placeholder="Choose a role...",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_role(sel: discord.Interaction):
                chosen_role = rs.values[0]
                await self.bot.users_col.update_one(
                    {"user_id": sel.user.id, "guild_id": sel.guild_id},
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
                verb = "locked" if chosen_type == "comp_role_lock" else "banned"
                emoji = "🎯" if chosen_type == "comp_role_lock" else "🚫"
                await sel.response.edit_message(
                    content=f"{emoji} **{chosen_role}** {verb}! It will apply on the next `/randomcomp` you're in.",
                    embed=None,
                    view=None,
                )

            rs.callback = on_role
            v = discord.ui.View(timeout=60)
            v.add_item(rs)
            v.add_item(_back_btn())
            prompt = (
                "🎯 Lock which role?"
                if chosen_type == "comp_role_lock"
                else "🚫 Ban which role?"
            )
            await interaction.response.edit_message(content=prompt, embed=None, view=v)
            return

        if chosen_type == "comp_agent_lock":
            rs = discord.ui.Select(
                placeholder="Choose a role first...",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_role_for_agent(sel: discord.Interaction):
                role = rs.values[0]
                ag_select = discord.ui.Select(
                    placeholder=f"Choose a {role} agent...",
                    options=[
                        discord.SelectOption(label=a, value=a) for a in AGENTS[role]
                    ],
                )

                async def on_agent(as_: discord.Interaction):
                    chosen_agent = ag_select.values[0]
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
                        content=f"🌟 **{chosen_agent}** locked! You'll get {role} + {chosen_agent} on the next comp.",
                        embed=None,
                        view=None,
                    )

                ag_select.callback = on_agent
                v2 = discord.ui.View(timeout=60)
                v2.add_item(ag_select)
                v2.add_item(_back_btn())
                await sel.response.edit_message(
                    content=f"🌟 Choose a **{role}** agent:", embed=None, view=v2
                )

            rs.callback = on_role_for_agent
            v = discord.ui.View(timeout=60)
            v.add_item(rs)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content="🌟 Choose the role first:", embed=None, view=v
            )
            return

        if chosen_type == "comp_weight":
            inv_item = next((i for i in items if i["type"] == "comp_weight"), None)
            shop_item = await self.bot.items_col.find_one(
                {"guild_id": interaction.guild_id, "name": inv_item["name"]}
                if inv_item
                else {"guild_id": interaction.guild_id, "type": "comp_weight"}
            )
            w_pct = int((shop_item or {}).get("weight_pct", 10))
            item_name = inv_item["name"] if inv_item else ""
            owned_count = sum(1 for i in items if i["type"] == "comp_weight")

            user_doc = await self.bot.users_col.find_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
            )
            cur = (user_doc or {}).get("active_comp_weights", [])
            cur_total = sum(w["weight"] for w in cur)

            if cur_total >= MAX_WEIGHT:
                await interaction.response.edit_message(
                    content=f"⚖️ Total weight pool is full ({cur_total}% / {MAX_WEIGHT}%).",
                    embed=None,
                    view=None,
                )
                return

            role_select = discord.ui.Select(
                placeholder="Which role do you want to weight toward?",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_weight_role(sel: discord.Interaction):
                chosen_role = role_select.values[0]
                remaining_cap = MAX_WEIGHT - cur_total
                max_usable = min(owned_count, remaining_cap // w_pct if w_pct else 1)
                if max_usable <= 0:
                    await sel.response.edit_message(
                        content=f"⚖️ Pool full ({cur_total}% / {MAX_WEIGHT}%).",
                        embed=None,
                        view=None,
                    )
                    return

                qty_options = [
                    discord.SelectOption(
                        label=f"×{n}  →  +{n * w_pct}% {chosen_role}  (pool total: {min(cur_total + n * w_pct, MAX_WEIGHT)}%)",
                        value=str(n),
                    )
                    for n in range(1, max_usable + 1)
                ]
                qty_select = discord.ui.Select(
                    placeholder=f"How many? ({remaining_cap}% pool remaining, each +{w_pct}%)",
                    options=qty_options,
                )

                async def on_qty(as_: discord.Interaction):
                    n = int(qty_select.values[0])
                    for _ in range(n):
                        await self.bot.users_col.update_one(
                            {"user_id": as_.user.id, "guild_id": as_.guild_id},
                            {
                                "$push": {
                                    "active_comp_weights": {
                                        "role": chosen_role,
                                        "weight": w_pct,
                                        "item_name": item_name,
                                    }
                                }
                            },
                            upsert=True,
                        )
                    new_total = min(cur_total + n * w_pct, MAX_WEIGHT)
                    await as_.response.edit_message(
                        content=f"⚖️ Stacked ×{n}! **{chosen_role}** +{n * w_pct}% (pool now {new_total}% / {MAX_WEIGHT}%).",
                        embed=None,
                        view=None,
                    )

                qty_select.callback = on_qty
                v2 = discord.ui.View(timeout=60)
                v2.add_item(qty_select)
                v2.add_item(_back_btn())
                await sel.response.edit_message(
                    content=f"⚖️ How many toward **{chosen_role}**?",
                    embed=None,
                    view=v2,
                )

            role_select.callback = on_weight_role
            cur_breakdown = (
                ", ".join(
                    f"{r}: {sum(w['weight'] for w in cur if (w.get('role') or w.get('value','')) == r)}%"
                    for r in ALL_ROLES
                    if any((w.get("role") or w.get("value", "")) == r for w in cur)
                )
                or "none"
            )
            v = discord.ui.View(timeout=60)
            v.add_item(role_select)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content=f"⚖️ Each **{item_name}** adds **+{w_pct}%** to any role (pool: {cur_total}% / {MAX_WEIGHT}% used).\nCurrent: {cur_breakdown}\nWhich role?",
                embed=None,
                view=v,
            )
            return

        if chosen_type == "comp_reduce":
            inv_item = next((i for i in items if i["type"] == "comp_reduce"), None)
            shop_item = await self.bot.items_col.find_one(
                {"guild_id": interaction.guild_id, "name": inv_item["name"]}
                if inv_item
                else {"guild_id": interaction.guild_id, "type": "comp_reduce"}
            )
            r_pct = int((shop_item or {}).get("reduce_pct", 20))
            item_name = inv_item["name"] if inv_item else ""
            owned_count = sum(1 for i in items if i["type"] == "comp_reduce")

            user_doc = await self.bot.users_col.find_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
            )
            cur_reds = (user_doc or {}).get("active_comp_reductions", [])

            role_select = discord.ui.Select(
                placeholder="Which role do you want to reduce?",
                options=[
                    discord.SelectOption(label=f"{ROLE_EMOJIS[r]} {r}", value=r)
                    for r in ALL_ROLES
                ],
            )

            async def on_reduce_role(sel: discord.Interaction):
                chosen_role = role_select.values[0]
                role_cur = sum(
                    w["weight"]
                    for w in cur_reds
                    if (w.get("role") or w.get("value", "")) == chosen_role
                )
                max_usable = min(owned_count, (100 - role_cur) // r_pct if r_pct else 1)
                if max_usable <= 0:
                    await sel.response.edit_message(
                        content=f"⬇️ **{chosen_role}** is already fully reduced.",
                        embed=None,
                        view=None,
                    )
                    return

                qty_options = [
                    discord.SelectOption(
                        label=f"×{n}  →  -{n * r_pct}% {chosen_role}  (total: {min(role_cur + n * r_pct, 100)}%)",
                        value=str(n),
                    )
                    for n in range(1, max_usable + 1)
                ]
                qty_select = discord.ui.Select(
                    placeholder=f"How many? (each -{r_pct}%)", options=qty_options
                )

                async def on_reduce_qty(as_: discord.Interaction):
                    n = int(qty_select.values[0])
                    for _ in range(n):
                        await self.bot.users_col.update_one(
                            {"user_id": as_.user.id, "guild_id": as_.guild_id},
                            {
                                "$push": {
                                    "active_comp_reductions": {
                                        "role": chosen_role,
                                        "weight": r_pct,
                                        "item_name": item_name,
                                    }
                                }
                            },
                            upsert=True,
                        )
                    await as_.response.edit_message(
                        content=f"⬇️ Reduced ×{n}! **{chosen_role}** -{n * r_pct}% probability.",
                        embed=None,
                        view=None,
                    )

                qty_select.callback = on_reduce_qty
                v2 = discord.ui.View(timeout=60)
                v2.add_item(qty_select)
                v2.add_item(_back_btn())
                await sel.response.edit_message(
                    content=f"⬇️ How many reductions on **{chosen_role}**?",
                    embed=None,
                    view=v2,
                )

            role_select.callback = on_reduce_role
            v = discord.ui.View(timeout=60)
            v.add_item(role_select)
            v.add_item(_back_btn())
            await interaction.response.edit_message(
                content=f"⬇️ Each **{item_name}** reduces a role by **{r_pct}%**. Which role?",
                embed=None,
                view=v,
            )
            return

    # ── /cancelitem ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="cancelitem", description="Cancel your currently queued comp items"
    )
    async def cancelitem(self, interaction: discord.Interaction):
        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        active = (user_doc or {}).get("active_comp_item")
        weights = (user_doc or {}).get("active_comp_weights", [])
        curses = (user_doc or {}).get("active_comp_curses", [])
        reds = (user_doc or {}).get("active_comp_reductions", [])
        creds = (user_doc or {}).get("active_comp_curse_reds", [])

        if not active and not weights and not curses and not reds and not creds:
            await interaction.response.send_message(
                "*you don't have any comp items queued.*", ephemeral=True
            )
            return

        await self.bot.users_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {
                "$unset": {
                    "active_comp_item": "",
                    "active_comp_weights": "",
                    "active_comp_curses": "",
                    "active_comp_reductions": "",
                    "active_comp_curse_reds": "",
                }
            },
        )
        await interaction.response.send_message(
            "❌ All queued comp items cleared — they're still in your inventory.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Valorant(bot))
