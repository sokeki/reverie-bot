"""
cogs/persistent_shop.py
─────────────────────────────────────────────────────────────────────────────
Permanent shop interface that lives in a Discord channel as a single message.

  /setshopchannel  – admin: designate a channel; posts (or re-posts) the shop
  /refreshshop     – admin: force-rebuild the embed and re-post if message gone

The message uses a *persistent* View (no timeout, stable custom_ids) so the
Browse & Buy button keeps working across restarts.  Clicking it opens an
ephemeral paginated select menu; the user picks an item and the purchase is
processed immediately.

External hook
─────────────
Call  await refresh_persistent_shop(bot, guild_id)  from shop.py after any
additem / removeitem / edititem so the embed stays current.
"""

from __future__ import annotations

import colorsys
import math

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import COLOUR_MAIN, COLOUR_LB, COLOUR_CONFIRM

# ── Colour naming ─────────────────────────────────────────────────────────────

_NAMED_COLOURS: list[tuple[str, tuple[int, int, int]]] = [
    ("crimson", (220, 20, 60)),
    ("red", (255, 0, 0)),
    ("coral", (255, 127, 80)),
    ("tomato", (255, 99, 71)),
    ("salmon", (250, 128, 114)),
    ("rose", (255, 102, 153)),
    ("pink", (255, 182, 193)),
    ("hot pink", (255, 105, 180)),
    ("deep pink", (255, 20, 147)),
    ("magenta", (255, 0, 255)),
    ("orange red", (255, 69, 0)),
    ("orange", (255, 165, 0)),
    ("dark orange", (255, 140, 0)),
    ("amber", (255, 191, 0)),
    ("gold", (255, 215, 0)),
    ("yellow", (255, 255, 0)),
    ("pale yellow", (255, 255, 153)),
    ("yellow green", (154, 205, 50)),
    ("lime", (0, 255, 0)),
    ("lime green", (50, 205, 50)),
    ("green", (0, 128, 0)),
    ("forest green", (34, 139, 34)),
    ("dark green", (0, 100, 0)),
    ("sea green", (46, 139, 87)),
    ("mint green", (152, 255, 152)),
    ("spring green", (0, 255, 127)),
    ("olive", (128, 128, 0)),
    ("teal", (0, 128, 128)),
    ("dark teal", (22, 70, 83)),
    ("cyan", (0, 255, 255)),
    ("sky blue", (135, 206, 235)),
    ("light blue", (173, 216, 230)),
    ("steel blue", (70, 130, 180)),
    ("cornflower blue", (100, 149, 237)),
    ("dodger blue", (30, 144, 255)),
    ("blue", (0, 0, 255)),
    ("royal blue", (65, 105, 225)),
    ("navy", (0, 0, 128)),
    ("slate blue", (106, 90, 205)),
    ("blue violet", (138, 43, 226)),
    ("indigo", (75, 0, 130)),
    ("purple", (128, 0, 128)),
    ("dark purple", (75, 0, 100)),
    ("medium purple", (147, 112, 219)),
    ("lavender", (200, 190, 240)),
    ("soft lavender", (220, 215, 245)),
    ("plum", (221, 160, 221)),
    ("violet", (238, 130, 238)),
    ("orchid", (218, 112, 214)),
    ("maroon", (128, 0, 0)),
    ("brown", (139, 69, 19)),
    ("sienna", (160, 82, 45)),
    ("chocolate", (210, 105, 30)),
    ("tan", (210, 180, 140)),
    ("peach", (255, 218, 185)),
    ("white", (255, 255, 255)),
    ("light gray", (211, 211, 211)),
    ("silver", (192, 192, 192)),
    ("gray", (128, 128, 128)),
    ("dark gray", (64, 64, 64)),
    ("charcoal", (54, 69, 79)),
    ("black", (0, 0, 0)),
]


def _colour_name(hex_val: int) -> str:
    """Return a human-readable colour name for a Discord role colour integer."""
    r = (hex_val >> 16) & 0xFF
    g = (hex_val >> 8) & 0xFF
    b = hex_val & 0xFF
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)

    best, best_dist = "", float("inf")
    for name, (nr, ng, nb) in _NAMED_COLOURS:
        nh, nl, ns = colorsys.rgb_to_hls(nr / 255, ng / 255, nb / 255)
        dh = min(abs(h - nh), 1 - abs(h - nh)) * 2  # circular hue distance
        dl = abs(l - nl)
        ds = abs(s - ns)
        sat_weight = (s + ns) / 2
        dist = (dh * sat_weight * 3) + (dl * 1.5) + (ds * 0.5)
        if dist < best_dist:
            best_dist = dist
            best = name
    return best


# ── Embed builder ─────────────────────────────────────────────────────────────

TYPE_EMOJI = {
    "role": "🎭",
    "title": "✨",
    "custom_title": "🖊️",
    "comp_role_lock": "🎯",
    "comp_role_ban": "🚫",
    "comp_agent_lock": "🌟",
    "comp_reroll": "🔄",
    "comp_role_swap": "🔀",
    "comp_weight": "⚖️",
    "comp_curse": "💀",
    "comp_reduce": "⬇️",
    "comp_curse_reduce": "🪄",
}

TYPE_LABEL = {
    "role": "🎭 Colour Role",
    "title": "✨ Title",
    "custom_title": "🖊️ Custom Title",
    "comp_role_lock": "🎯 Role Lock  *(consumable)*",
    "comp_role_ban": "🚫 Role Ban  *(consumable)*",
    "comp_agent_lock": "🌟 Agent Lock  *(consumable)*",
    "comp_reroll": "🔄 Role Reroll  *(consumable)*",
    "comp_role_swap": "🔀 Role Swap  *(consumable)*",
    "comp_weight": "⚖️ Role Weight  *(stackable consumable)*",
    "comp_curse": "💀 Role Curse  *(consumable)*",
    "comp_reduce": "⬇️ Role Reduction  *(stackable consumable)*",
    "comp_curse_reduce": "🪄 Curse Reduction  *(consumable)*",
}

# Valorant data mirrored here so the preview can show it without importing valorant.py
_VAL_ROLES = ["Duelist", "Initiator", "Controller", "Sentinel"]
_VAL_ROLE_EMOJIS = {
    "Duelist": "⚔️",
    "Initiator": "🔍",
    "Controller": "🌫️",
    "Sentinel": "🛡️",
}
_VAL_AGENTS: dict[str, list[str]] = {
    "Duelist": ["Jett", "Reyna", "Raze", "Phoenix", "Yoru", "Neon", "Iso", "Waylay"],
    "Initiator": ["Sova", "Breach", "Skye", "KAY/O", "Fade", "Gekko"],
    "Controller": ["Brimstone", "Viper", "Omen", "Astra", "Harbor", "Clove"],
    "Sentinel": ["Sage", "Cypher", "Killjoy", "Chamber", "Deadlock", "Vyse"],
}
_VAL_ROLE_COLOURS = {
    "Duelist": 0xFF4655,
    "Initiator": 0x4CAF50,
    "Controller": 0x9C27B0,
    "Sentinel": 0x2196F3,
}

ITEMS_PER_PAGE = 10


def _build_embed(
    items: list[dict], guild: discord.Guild, page: int = 0
) -> discord.Embed:
    """Pinned channel embed — clean overview with category counts only."""
    embed = discord.Embed(
        title="🌙 The Dream Shop",
        description="*spend your dream points on something wonderful...*\nClick a category below to browse and buy.",
        color=COLOUR_LB,
    )

    if not items:
        embed.description = (
            "*the shop shelves are bare for now...  check back soon!* 🌫️"
        )
        embed.set_footer(text=f"Reverie  •  {guild.name}")
        return embed

    lines = []
    for label, emoji, types in CATEGORIES:
        cat_items = [i for i in items if i["type"] in types]
        if not cat_items:
            continue
        min_cost = min(i["cost"] for i in cat_items)
        max_cost = max(i["cost"] for i in cat_items)
        cost_str = (
            f"✨ {min_cost:,}"
            if min_cost == max_cost
            else f"✨ {min_cost:,}–{max_cost:,} pts"
        )
        lines.append(
            f"{emoji} **{label}** — {len(cat_items)} item{'s' if len(cat_items) != 1 else ''}  ·  {cost_str}"
        )

    if lines:
        embed.description += "\n\n" + "\n".join(lines)

    embed.set_footer(text=f"Reverie  •  {guild.name}")
    return embed


# ── Persistent shop view (lives on the channel message) ──────────────────────


class PersistentShopView(discord.ui.View):
    """
    One button per category, each with a stable custom_id so Discord
    re-attaches them automatically on every restart.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎭 Colour Roles",
        style=discord.ButtonStyle.secondary,
        custom_id="persistent_shop:cat:role",
    )
    async def cat_roles(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._open(interaction, "Colour Roles")

    @discord.ui.button(
        label="✨ Titles",
        style=discord.ButtonStyle.secondary,
        custom_id="persistent_shop:cat:title",
    )
    async def cat_titles(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._open(interaction, "Titles")

    @discord.ui.button(
        label="🎮 Comp Items",
        style=discord.ButtonStyle.secondary,
        custom_id="persistent_shop:cat:comp",
    )
    async def cat_comp(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._open(interaction, "Comp Items")

    async def _open(self, interaction: discord.Interaction, category_label: str):
        cog: PersistentShop | None = interaction.client.cogs.get("PersistentShop")
        if cog is None:
            await interaction.response.send_message(
                "⚠️ Shop is temporarily unavailable.", ephemeral=True
            )
            return
        await cog._show_category(interaction, category_label)


# ── Category definitions ──────────────────────────────────────────────────────

# Each category: (label, emoji, set of type strings it contains)
CATEGORIES: list[tuple[str, str, set[str]]] = [
    ("Colour Roles", "🎭", {"role"}),
    ("Titles", "✨", {"title", "custom_title"}),
    (
        "Comp Items",
        "🎮",
        {
            "comp_role_lock",
            "comp_role_ban",
            "comp_agent_lock",
            "comp_reroll",
            "comp_role_swap",
            "comp_weight",
            "comp_curse",
            "comp_reduce",
            "comp_curse_reduce",
        },
    ),
]


def _categories_with_items(items: list[dict]) -> list[tuple[str, str, set[str]]]:
    """Return only categories that have at least one item in stock."""
    return [
        (label, emoji, types)
        for label, emoji, types in CATEGORIES
        if any(i["type"] in types for i in items)
    ]


def _category_embed(balance: int, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🛒 Browse the Dream Shop",
        description="*what are you looking for?*",
        color=COLOUR_MAIN,
    )
    embed.set_footer(text=f"Your balance: {balance:,} pts  •  Reverie  •  {guild.name}")
    return embed


class CategorySelect(discord.ui.Select):
    def __init__(self, available: list[tuple[str, str, set[str]]]):
        options = [
            discord.SelectOption(label=label, value=label, emoji=emoji)
            for label, emoji, _ in available
        ]
        super().__init__(placeholder="Choose a category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cog: PersistentShop | None = interaction.client.cogs.get("PersistentShop")
        if cog is None:
            await interaction.response.send_message(
                "⚠️ Shop unavailable.", ephemeral=True
            )
            return
        v: CategoryView = self.view
        category_label = self.values[0]
        # Find the type set for the chosen category
        types = next(types for label, _, types in CATEGORIES if label == category_label)
        filtered = [i for i in v.all_items if i["type"] in types]
        browse_view = BuyMenuView(
            filtered,
            interaction.guild,
            page=0,
            balance=v.balance,
            all_items=v.all_items,
            category_label=category_label,
        )
        await interaction.response.edit_message(
            embed=browse_view._menu_embed(), view=browse_view
        )


class CategoryView(discord.ui.View):
    def __init__(self, all_items: list[dict], guild: discord.Guild, balance: int):
        super().__init__(timeout=90)
        self.all_items = all_items
        self.guild = guild
        self.balance = balance
        available = _categories_with_items(all_items)
        self.add_item(CategorySelect(available))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ── Ephemeral buy menu (paginated) ────────────────────────────────────────────


class ItemSelect(discord.ui.Select):
    """Dropdown shown on the browse page. Selecting an item opens a preview."""

    def __init__(self, page_items: list[dict], guild: discord.Guild):
        options = []
        for item in page_items:
            emoji = TYPE_EMOJI.get(item["type"], "•")
            desc_parts = [f"{item['cost']:,} pts"]
            if item["type"] == "role" and item.get("role_id"):
                role = guild.get_role(item["role_id"])
                if role and role.colour.value:
                    desc_parts.append(_colour_name(role.colour.value))
            options.append(
                discord.SelectOption(
                    label=item["name"][:100],
                    value=item["name"],
                    description="  •  ".join(desc_parts)[:100],
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder="Select an item to preview...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        cog: PersistentShop | None = interaction.client.cogs.get("PersistentShop")
        if cog is None:
            await interaction.response.send_message(
                "⚠️ Shop unavailable.", ephemeral=True
            )
            return
        v: BuyMenuView = self.view
        await cog._show_preview(
            interaction,
            self.values[0],
            filtered_items=v.items,
            all_items=v.all_items,
            browse_page=v.page,
            balance=v.balance,
            category_label=v.category_label,
        )


class BuyMenuView(discord.ui.View):
    """Ephemeral paginated browse menu — selecting an item goes to PreviewView."""

    def __init__(
        self,
        items: list[dict],  # filtered items for this category
        guild: discord.Guild,
        page: int = 0,
        balance: int = 0,
        all_items: list[dict] | None = None,  # full unfiltered list for Back→category
        category_label: str = "",
    ):
        super().__init__(timeout=90)
        self.items = items
        self.guild = guild
        self.page = page
        self.balance = balance
        self.all_items = all_items if all_items is not None else items
        self.category_label = category_label
        self.total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        page_items = self.items[
            self.page * ITEMS_PER_PAGE : (self.page + 1) * ITEMS_PER_PAGE
        ]
        self.add_item(ItemSelect(page_items, self.guild))

        # Navigation row
        if self.total_pages > 1:
            prev = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0),
                row=1,
            )
            next_ = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.total_pages - 1),
                row=1,
            )
            prev.callback = self._prev
            next_.callback = self._next
            self.add_item(prev)
            self.add_item(next_)

        back_btn = discord.ui.Button(
            label="◀ Categories",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        back_btn.callback = self._back_to_categories
        self.add_item(back_btn)

    def _menu_embed(self) -> discord.Embed:
        cat_label = f" — {self.category_label}" if self.category_label else ""
        embed = discord.Embed(
            title=f"🛒 Dream Shop{cat_label}",
            description="*select an item from the dropdown to preview and buy.*",
            color=COLOUR_MAIN,
        )
        page_items = self.items[
            self.page * ITEMS_PER_PAGE : (self.page + 1) * ITEMS_PER_PAGE
        ]
        for item in page_items:
            emoji = TYPE_EMOJI.get(item["type"], "•")
            colour_str = ""
            if item["type"] == "role" and item.get("role_id"):
                role = self.guild.get_role(item["role_id"])
                if role and role.colour.value:
                    cname = _colour_name(role.colour.value)
                    colour_str = f"\n🎨 {cname}  (`#{role.colour.value:06X}`)"
            desc = item.get("description") or "no description"
            embed.add_field(
                name=f"{emoji}  {item['name']}  —  ✨ {item['cost']:,} pts{colour_str}",
                value=desc,
                inline=False,
            )
        embed.set_footer(
            text=f"Your balance: {self.balance:,} pts  •  "
            f"Page {self.page + 1}/{self.total_pages}  •  Reverie"
        )
        return embed

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self._menu_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self._menu_embed(), view=self)

    async def _back_to_categories(self, interaction: discord.Interaction):
        view = CategoryView(self.all_items, interaction.guild, self.balance)
        await interaction.response.edit_message(
            embed=_category_embed(self.balance, interaction.guild), view=view
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ── Preview screen ────────────────────────────────────────────────────────────

# ── Per-type preview builders ─────────────────────────────────────────────────


def _afford_line(
    item: dict, balance: int, already_owned: bool, consumable: bool = False
) -> str:
    if already_owned and not consumable:
        return "📦 You already own this item"
    if balance >= item["cost"]:
        return f"✅ You can afford this  ({balance:,} pts available)"
    return (
        f"❌ You need **{item['cost'] - balance:,}** more pts  ({balance:,} available)"
    )


def _preview_role(
    item: dict, guild: discord.Guild, balance: int, already_owned: bool
) -> discord.Embed:
    embed_colour = COLOUR_MAIN
    colour_line = ""
    discord_role = (
        guild.get_role(item.get("role_id", 0)) if item.get("role_id") else None
    )
    if discord_role and discord_role.colour.value:
        embed_colour = discord_role.colour.value
        cname = _colour_name(discord_role.colour.value)
        colour_line = (
            f"\n🎨 **Colour:** {cname}  (`#{discord_role.colour.value:06X}`)\n"
        )
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"🎭  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['role']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts"
            f"{colour_line}\n"
            f"Purchasing grants you the **{discord_role.name if discord_role else item['name']}** "
            f"Discord role, which changes your name colour in the server.\n\n"
            f"{_afford_line(item, balance, already_owned)}"
        ),
        color=embed_colour,
    )
    return embed


def _preview_title(item: dict, balance: int, already_owned: bool) -> discord.Embed:
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"✨  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['title']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Once purchased, use `/settitle` to equip it. "
            f"It will appear on your `/points` profile like this:\n\n"
            f"> 🌙 **YourName**\n> ✨ *{item['name']}*\n\n"
            f"{_afford_line(item, balance, already_owned)}"
        ),
        color=COLOUR_LB,
    )
    return embed


def _preview_custom_title(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"🖊️  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['custom_title']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"A one-use item. After purchasing, use `/setcustomtitle <text>` to set "
            f"any title you like (up to 32 characters). It will appear on your `/points` "
            f"profile and consumes this item.\n\n"
            f"{_afford_line(item, balance, already_owned)}"
        ),
        color=COLOUR_LB,
    )
    return embed


def _preview_comp_role_lock(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    roles_str = "  ".join(f"{_VAL_ROLE_EMOJIS[r]} **{r}**" for r in _VAL_ROLES)
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"🎯  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_role_lock']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Before a `/randomcomp` is rolled, use `/useitem` to activate this. "
            f"You'll be guaranteed to receive the role of your choice — no matter how "
            f"the shuffle lands.\n\n"
            f"**Available roles:**\n{roles_str}\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0xFFD700,
    )
    return embed


def _preview_comp_role_ban(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    roles_str = "  ".join(f"{_VAL_ROLE_EMOJIS[r]} **{r}**" for r in _VAL_ROLES)
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"🚫  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_role_ban']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Before a `/randomcomp` is rolled, use `/useitem` to activate this. "
            f"Pick one role to exclude — the bot will never assign it to you in that roll. "
            f"If all remaining roles are taken, you'll get Free Pick.\n\n"
            f"**Bannable roles:**\n{roles_str}\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0xFF4655,
    )
    return embed


def _preview_comp_agent_lock(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    desc = item.get("description") or "no description"
    agent_lines = "\n".join(
        f"{_VAL_ROLE_EMOJIS[role]} **{role}:** {', '.join(agents)}"
        for role, agents in _VAL_AGENTS.items()
    )
    embed = discord.Embed(
        title=f"🌟  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_agent_lock']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Before a `/randomcomp` is rolled, use `/useitem` to activate this. "
            f"Pick any agent — you'll be assigned that agent *and* their role automatically, "
            f"regardless of the shuffle.\n\n"
            f"**All agents:**\n{agent_lines}\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0x4CAF50,
    )
    return embed


def _preview_comp_reroll(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"🔄  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_reroll']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Activate with `/useitem` or from the pre-roll screen. After the comp result "
            f"posts, a **🔄 Reshuffle** button appears — clicking it randomly redistributes "
            f"all **unlocked** players among the remaining roles. Players who used a Role Lock "
            f"or Agent Lock keep their slot; everyone else gets reshuffled.\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0x9C27B0,
    )
    return embed


def _preview_comp_role_swap(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    desc = item.get("description") or "no description"
    embed = discord.Embed(
        title=f"🔀  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_role_swap']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Activate with `/useitem` before a `/randomcomp`. After the comp result posts, "
            f"a **🔀 Swap** button will appear for you — click it to pick another player "
            f"and swap roles with them. The swap is instant and shown publicly.\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0x00BCD4,
    )
    return embed


def _preview_comp_weight(
    item: dict, balance: int, already_owned: bool
) -> discord.Embed:
    desc = item.get("description") or "no description"
    weight_pct = item.get("weight_pct", "?")
    roles_str = "  ".join(f"{_VAL_ROLE_EMOJIS[r]} **{r}**" for r in _VAL_ROLES)
    embed = discord.Embed(
        title=f"⚖️  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_weight']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Activate via the pre-roll screen or `/useitem`. You'll pick which role to weight toward "
            f"at activation time. Each use adds **+{weight_pct}%** to your chosen role's probability.\n\n"
            f"**Shared pool:** all your weights across all roles share a 95% cap — so 30% Duelist + 40% "
            f"Initiator = 70% used, 25% left to distribute. Stack multiple items to build up toward any role.\n\n"
            f"**Roles:**\n{roles_str}\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0xFF9800,
    )
    return embed


def _preview_comp_curse(item: dict, balance: int, already_owned: bool) -> discord.Embed:
    desc = item.get("description") or "no description"
    curse_pct = item.get("curse_pct", "?")
    roles_str = "  ".join(f"{_VAL_ROLE_EMOJIS[r]} **{r}**" for r in _VAL_ROLES)
    embed = discord.Embed(
        title=f"💀  {item['name']}",
        description=(
            f"*{desc}*\n\n"
            f"**Type:** {TYPE_LABEL['comp_curse']}\n"
            f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
            f"Activate from the pre-roll screen when running `/randomcomp`. You'll pick a target "
            f"player and which role to push them toward. Each use adds **+{curse_pct}%** toward "
            f"your chosen role for that target.\n\n"
            f"**Stackable** — multiple curses on the same target/role add up (capped at 100%). "
            f"You can even spread curses across multiple targets in the same comp.\n\n"
            f"**Roles:**\n{roles_str}\n\n"
            f"⚠️ Consumed on use. Can own multiple.\n\n"
            f"{_afford_line(item, balance, already_owned, consumable=True)}"
        ),
        color=0x333333,
    )
    return embed


# ── Preview dispatcher ────────────────────────────────────────────────────────


def _build_preview_embed(
    item: dict,
    guild: discord.Guild,
    balance: int,
    already_owned: bool,
) -> discord.Embed:
    """Dispatch to the right per-type preview builder."""
    t = item.get("type", "")
    if t == "role":
        embed = _preview_role(item, guild, balance, already_owned)
    elif t == "title":
        embed = _preview_title(item, balance, already_owned)
    elif t == "custom_title":
        embed = _preview_custom_title(item, balance, already_owned)
    elif t == "comp_role_lock":
        embed = _preview_comp_role_lock(item, balance, already_owned)
    elif t == "comp_role_ban":
        embed = _preview_comp_role_ban(item, balance, already_owned)
    elif t == "comp_agent_lock":
        embed = _preview_comp_agent_lock(item, balance, already_owned)
    elif t == "comp_reroll":
        embed = _preview_comp_reroll(item, balance, already_owned)
    elif t == "comp_role_swap":
        embed = _preview_comp_role_swap(item, balance, already_owned)
    elif t == "comp_weight":
        embed = _preview_comp_weight(item, balance, already_owned)
    elif t == "comp_curse":
        embed = _preview_comp_curse(item, balance, already_owned)
    else:
        # Fallback for unknown future types
        desc = item.get("description") or "no description"
        embed = discord.Embed(
            title=f"•  {item['name']}",
            description=(
                f"*{desc}*\n\n"
                f"**Cost:** ✨ {item['cost']:,} dream pts\n\n"
                f"{_afford_line(item, balance, already_owned)}"
            ),
            color=COLOUR_MAIN,
        )
    embed.set_footer(text=f"Reverie  •  {guild.name}")
    return embed


class PreviewView(discord.ui.View):
    """Shown after selecting an item. Has Buy + Back buttons."""

    def __init__(
        self,
        item: dict,
        filtered_items: list[dict],  # items in the current category
        all_items: list[dict],  # full list for returning to category screen
        guild: discord.Guild,
        browse_page: int,
        balance: int,
        already_owned: bool,
        category_label: str = "",
    ):
        super().__init__(timeout=90)
        self.item = item
        self.filtered_items = filtered_items
        self.all_items = all_items
        self.guild = guild
        self.browse_page = browse_page
        self.balance = balance
        self.already_owned = already_owned
        self.category_label = category_label

        buy_btn = discord.ui.Button(
            label="✨ Buy",
            style=discord.ButtonStyle.success,
            disabled=(balance < item["cost"] or already_owned),
            row=0,
        )
        back_btn = discord.ui.Button(
            label="◀ Back",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        buy_btn.callback = self._buy
        back_btn.callback = self._back
        self.add_item(buy_btn)
        self.add_item(back_btn)

    async def _buy(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog: PersistentShop | None = interaction.client.cogs.get("PersistentShop")
        if cog is None:
            await interaction.followup.send("⚠️ Shop unavailable.", ephemeral=True)
            return
        await cog._process_purchase(interaction, self.item["name"])

    async def _back(self, interaction: discord.Interaction):
        view = BuyMenuView(
            self.filtered_items,
            interaction.guild,
            page=self.browse_page,
            balance=self.balance,
            all_items=self.all_items,
            category_label=self.category_label,
        )
        await interaction.response.edit_message(embed=view._menu_embed(), view=view)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────


class PersistentShop(commands.Cog):
    """Permanent shop interface pinned to a channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(PersistentShopView())
        self._dashboard_refresh_task.start()

    def cog_unload(self):
        self._dashboard_refresh_task.cancel()

    # ── Dashboard-triggered refresh (polls MongoDB flag every 30s) ────────────

    @tasks.loop(seconds=30)
    async def _dashboard_refresh_task(self):
        """Check if the dashboard set a refresh flag and act on it."""
        try:
            doc = await self.bot.settings_col.find_one({"shop_refresh_pending": True})
            if not doc:
                return
            guild_id = doc.get("guild_id")
            await self.bot.settings_col.update_one(
                {"guild_id": guild_id},
                {"$unset": {"shop_refresh_pending": ""}},
            )
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self._post_or_edit_shop(guild)
        except Exception as e:
            print(f"[Shop] Dashboard refresh check error: {e}")

    @_dashboard_refresh_task.before_loop
    async def _before_refresh_task(self):
        await self.bot.wait_until_ready()

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _fetch_items(self, guild_id: int) -> list[dict]:
        items = await self.bot.items_col.find({"guild_id": guild_id}).to_list(
            length=200
        )
        items.sort(key=lambda i: (i.get("cost", 0), i.get("name", "").lower()))
        return items

    async def _get_settings(self, guild_id: int) -> dict:
        return await self.bot.settings_col.find_one({"guild_id": guild_id}) or {}

    async def _save_settings(self, guild_id: int, update: dict):
        await self.bot.settings_col.update_one(
            {"guild_id": guild_id}, {"$set": update}, upsert=True
        )

    # ── Core: post or edit the permanent message ──────────────────────────────

    async def _post_or_edit_shop(self, guild: discord.Guild) -> bool:
        settings = await self._get_settings(guild.id)
        channel_id = settings.get("shop_channel_id")
        message_id = settings.get("shop_message_id")

        if not channel_id:
            return False

        channel = guild.get_channel(channel_id)
        if channel is None:
            return False

        items = await self._fetch_items(guild.id)
        embed = _build_embed(items, guild, page=0)
        view = PersistentShopView()

        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed, view=view)
                return True
            except (discord.NotFound, discord.Forbidden):
                pass

        try:
            msg = await channel.send(embed=embed, view=view)
            await self._save_settings(guild.id, {"shop_message_id": msg.id})
            return True
        except discord.Forbidden:
            return False

    # ── Buy flow ──────────────────────────────────────────────────────────────

    async def _show_category(
        self, interaction: discord.Interaction, category_label: str
    ):
        """Open the ephemeral browse menu directly at a specific category."""
        all_items = await self._fetch_items(interaction.guild_id)
        if not all_items:
            await interaction.response.send_message(
                "*the shop is empty right now...* 🌫️", ephemeral=True
            )
            return

        types = next(
            (types for label, _, types in CATEGORIES if label == category_label), None
        )
        filtered = [i for i in all_items if i["type"] in types] if types else all_items

        if not filtered:
            await interaction.response.send_message(
                f"*no items in **{category_label}** right now.*", ephemeral=True
            )
            return

        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        balance = user_doc.get("points", 0) if user_doc else 0

        view = BuyMenuView(
            filtered,
            interaction.guild,
            page=0,
            balance=balance,
            all_items=all_items,
            category_label=category_label,
        )
        await interaction.response.send_message(
            embed=view._menu_embed(), view=view, ephemeral=True
        )

    async def _show_buy_menu(self, interaction: discord.Interaction, page: int = 0):
        """Open the ephemeral category picker (used by /shop command fallback)."""
        items = await self._fetch_items(interaction.guild_id)
        if not items:
            await interaction.response.send_message(
                "*the shop is empty right now...* 🌫️", ephemeral=True
            )
            return

        user_doc = await self.bot.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        balance = user_doc.get("points", 0) if user_doc else 0

        view = CategoryView(items, interaction.guild, balance)
        await interaction.response.send_message(
            embed=_category_embed(balance, interaction.guild), view=view, ephemeral=True
        )

    async def _show_preview(
        self,
        interaction: discord.Interaction,
        item_name: str,
        filtered_items: list[dict],
        all_items: list[dict],
        browse_page: int,
        balance: int,
        category_label: str = "",
    ):
        """Edit the ephemeral message to show a rich preview of the selected item."""
        item = next(
            (i for i in filtered_items if i["name"].lower() == item_name.lower()), None
        )
        if item is None:
            await interaction.response.send_message(
                f"⚠️ **{item_name}** no longer exists in the shop.", ephemeral=True
            )
            return

        CONSUMABLE_TYPES = {
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
        already_owned = False
        if item["type"] not in CONSUMABLE_TYPES:
            inv_doc = await self.bot.inv_col.find_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
            )
            inventory = inv_doc.get("items", []) if inv_doc else []
            already_owned = any(
                i["name"].lower() == item["name"].lower() for i in inventory
            )

        embed = _build_preview_embed(item, interaction.guild, balance, already_owned)
        view = PreviewView(
            item,
            filtered_items,
            all_items,
            interaction.guild,
            browse_page,
            balance,
            already_owned,
            category_label=category_label,
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _process_purchase(self, interaction: discord.Interaction, item_name: str):
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        shop_item = await self.bot.items_col.find_one(
            {
                "guild_id": guild_id,
                "name": {"$regex": f"^{item_name}$", "$options": "i"},
            }
        )
        if not shop_item:
            await interaction.followup.send(
                f"⚠️ **{item_name}** no longer exists in the shop.", ephemeral=True
            )
            return

        CONSUMABLE_TYPES = {
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
        if shop_item["type"] not in CONSUMABLE_TYPES:
            inv_doc = await self.bot.inv_col.find_one(
                {"user_id": user_id, "guild_id": guild_id}
            )
            inventory = inv_doc.get("items", []) if inv_doc else []
            if any(i["name"].lower() == shop_item["name"].lower() for i in inventory):
                await interaction.followup.send(
                    f"*you already own **{shop_item['name']}**!* ✨", ephemeral=True
                )
                return

        user_doc = await self.bot.users_col.find_one(
            {"user_id": user_id, "guild_id": guild_id}
        )
        balance = user_doc.get("points", 0) if user_doc else 0
        if balance < shop_item["cost"]:
            shortage = shop_item["cost"] - balance
            await interaction.followup.send(
                f"*not enough dream points...* you need **{shortage:,}** more "
                f"to buy **{shop_item['name']}**. 🌫️",
                ephemeral=True,
            )
            return

        await self.bot.users_col.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$inc": {"points": -shop_item["cost"]}},
        )

        if shop_item["type"] == "role":
            role = interaction.guild.get_role(shop_item["role_id"])
            if not role:
                await self.bot.users_col.update_one(
                    {"user_id": user_id, "guild_id": guild_id},
                    {"$inc": {"points": shop_item["cost"]}},
                )
                await interaction.followup.send(
                    "⚠️ That role no longer exists. Points refunded.", ephemeral=True
                )
                return
            try:
                await interaction.user.add_roles(role, reason="Reverie shop purchase")
            except discord.Forbidden:
                await self.bot.users_col.update_one(
                    {"user_id": user_id, "guild_id": guild_id},
                    {"$inc": {"points": shop_item["cost"]}},
                )
                await interaction.followup.send(
                    "⚠️ Missing permissions to assign that role. Points refunded.",
                    ephemeral=True,
                )
                return

        await self.bot.inv_col.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {
                "$push": {
                    "items": {"name": shop_item["name"], "type": shop_item["type"]}
                }
            },
            upsert=True,
        )

        embed = discord.Embed(
            title="✨ Purchase Complete",
            description=f"**{shop_item['name']}** is now yours, {interaction.user.mention}.",
            color=COLOUR_CONFIRM,
        )
        if shop_item["type"] == "custom_title":
            embed.description += "\n\nUse `/setcustomtitle` to set your title."
        elif shop_item["type"] == "title":
            embed.description += "\n\nUse `/settitle` to equip it on your profile."
        elif shop_item["type"] in (
            "comp_role_lock",
            "comp_role_ban",
            "comp_agent_lock",
            "comp_reroll",
            "comp_role_swap",
            "comp_weight",
        ):
            embed.description += (
                "\n\nUse `/useitem` before the next `/randomcomp` to activate it."
            )
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(
        name="setshopchannel",
        description="[Admin] Set the channel for the permanent shop interface",
    )
    @app_commands.describe(channel="The channel to post the shop in")
    @app_commands.default_permissions(administrator=True)
    async def setshopchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await interaction.response.defer(ephemeral=True)
        await self._save_settings(
            interaction.guild_id,
            {"shop_channel_id": channel.id, "shop_message_id": None},
        )
        ok = await self._post_or_edit_shop(interaction.guild)
        if ok:
            await interaction.followup.send(
                f"🌙 Permanent shop posted in {channel.mention}. "
                "It will auto-update whenever items are changed.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Saved the channel but couldn't post — make sure I have "
                f"**Send Messages** and **Embed Links** in {channel.mention}.",
                ephemeral=True,
            )

    @app_commands.command(
        name="refreshshop",
        description="[Admin] Force-refresh the permanent shop embed",
    )
    @app_commands.default_permissions(administrator=True)
    async def refreshshop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok = await self._post_or_edit_shop(interaction.guild)
        if ok:
            await interaction.followup.send("🌙 Shop refreshed.", ephemeral=True)
        else:
            await interaction.followup.send(
                "⚠️ No shop channel set. Use `/setshopchannel` first.",
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._post_or_edit_shop(guild)


# ── Public refresh hook ───────────────────────────────────────────────────────


async def refresh_persistent_shop(bot: commands.Bot, guild_id: int):
    cog: PersistentShop | None = bot.cogs.get("PersistentShop")
    if cog is None:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    await cog._post_or_edit_shop(guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(PersistentShop(bot))
