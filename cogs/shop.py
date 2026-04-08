import re
import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_MAIN, COLOUR_LB, COLOUR_CONFIRM


# ── Helpers ───────────────────────────────────────────────────────────────────

CUSTOM_TITLE_MAX_LEN = 32
CUSTOM_TITLE_PATTERN = re.compile(r"^[a-zA-Z0-9 '\-!?.]+$")


def _item_type_label(item_type: str) -> str:
    return {
        "role": "🎭 Role",
        "title": "✨ Title",
        "role_remover": "🗑️ Role Remover",
        "custom_title": "🖊️ Custom Title",
    }.get(item_type, item_type.capitalize())


async def _get_item(items_col, guild_id: int, name: str) -> dict | None:
    return await items_col.find_one(
        {"guild_id": guild_id, "name": {"$regex": f"^{name}$", "$options": "i"}}
    )


async def _get_inventory(inv_col, user_id: int, guild_id: int) -> list:
    doc = await inv_col.find_one({"user_id": user_id, "guild_id": guild_id})
    return doc.get("items", []) if doc else []


# ── Role remover select menu ───────────────────────────────────────────────────


class RoleRemoverSelect(discord.ui.Select):
    def __init__(self, purchasable_roles: list[discord.Role]):
        options = [
            discord.SelectOption(
                label=role.name,
                value=str(role.id),
                description=f"Remove the {role.name} role",
                emoji="🎭",
            )
            for role in purchasable_roles
        ]
        super().__init__(
            placeholder="Choose a role to remove...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        role = interaction.guild.get_role(role_id)

        if not role:
            await interaction.response.send_message(
                "⚠️ That role no longer exists on the server.",
                ephemeral=True,
            )
            return

        try:
            await interaction.user.remove_roles(
                role, reason="Reverie role remover used"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I don't have permission to remove that role. Please let an admin know!",
                ephemeral=True,
            )
            return

        # Remove the role from inventory
        await self.view.inv_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {
                "$pull": {
                    "items": {
                        "type": "role",
                        "name": self.view.role_item_names.get(role_id, ""),
                    }
                }
            },
        )

        # Consume one role remover from inventory
        await self.view.inv_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {
                "$pull": {
                    "items": {"type": "role_remover", "name": self.view.remover_name}
                }
            },
        )

        embed = discord.Embed(
            title="🗑️ Role Removed",
            description=f"The **{role.name}** role has been removed from your profile.",
            color=COLOUR_CONFIRM,
        )
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.response.edit_message(embed=embed, view=None)


class RoleRemoverView(discord.ui.View):
    def __init__(
        self,
        purchasable_roles: list[discord.Role],
        inv_col,
        role_item_names: dict,
        remover_name: str,
    ):
        super().__init__(timeout=60)
        self.inv_col = inv_col
        self.role_item_names = role_item_names  # role_id -> item name in inventory
        self.remover_name = remover_name
        self.add_item(RoleRemoverSelect(purchasable_roles))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Shop paginator ───────────────────────────────────────────────────────────

ITEMS_PER_PAGE = 10


def _build_shop_embed(
    items: list, page: int, total_pages: int, guild: discord.Guild
) -> discord.Embed:
    embed = discord.Embed(
        title="🛒 The Dream Shop",
        description="*spend your dream points on something wonderful...*",
        color=COLOUR_LB,
    )
    start = page * ITEMS_PER_PAGE
    for item in items[start : start + ITEMS_PER_PAGE]:
        label = _item_type_label(item["type"])
        colour_str = ""
        if item["type"] == "role" and item.get("role_id"):
            role = guild.get_role(item["role_id"])
            if role and role.colour.value:
                colour_str = f"  •  🎨 `#{role.colour.value:06X}`"
        embed.add_field(
            name=f"{item['name']}  —  ✨ {item['cost']:,} pts",
            value=f"{label}{colour_str}  •  {item.get('description', 'no description')}",
            inline=False,
        )
    embed.set_footer(
        text=f"Page {page + 1} of {total_pages}  •  /buy <item> to purchase  •  /rolepreview <item> for colour preview  •  Reverie"
    )
    return embed


class ShopView(discord.ui.View):
    def __init__(self, items: list, guild: discord.Guild):
        super().__init__(timeout=120)
        self.items = items
        self.guild = guild
        self.page = 0
        self.total_pages = max(1, -(-len(items) // ITEMS_PER_PAGE))  # ceiling division
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def current_embed(self) -> discord.Embed:
        return _build_shop_embed(self.items, self.page, self.total_pages, self.guild)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────


class Shop(commands.Cog):
    """Dream shop — spend your points on roles and titles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.items_col = bot.items_col
        self.inv_col = bot.inv_col
        self.users_col = bot.users_col

    # ── /shop ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="shop", description="Browse the dream shop")
    async def shop(self, interaction: discord.Interaction):
        items = await self.items_col.find({"guild_id": interaction.guild_id}).to_list(
            length=200
        )
        items.sort(key=lambda i: (i.get("cost", 0), i.get("name", "").lower()))

        if not items:
            await interaction.response.send_message(
                "*the shop shelves are bare...* an admin can add items with `/additem`. 🌫️",
                ephemeral=True,
            )
            return

        view = ShopView(items, interaction.guild)
        embed = view.current_embed()
        await interaction.response.send_message(embed=embed, view=view)

    # ── /rolepreview ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="rolepreview",
        description="Preview the colour of a role item from the shop",
    )
    @app_commands.describe(item="Name of the role item to preview")
    async def rolepreview(self, interaction: discord.Interaction, item: str):
        shop_item = await _get_item(self.items_col, interaction.guild_id, item)

        if not shop_item:
            await interaction.response.send_message(
                f"*no item called **{item}** found in the shop.* Check `/shop` for available items.",
                ephemeral=True,
            )
            return

        if shop_item["type"] != "role":
            await interaction.response.send_message(
                f"**{shop_item['name']}** is not a role — no colour to preview!",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(shop_item.get("role_id"))
        if not role:
            await interaction.response.send_message(
                "⚠️ That role no longer exists on the server.",
                ephemeral=True,
            )
            return

        colour = role.colour if role.colour.value else discord.Colour(COLOUR_LB)
        hex_str = f"#{role.colour.value:06X}" if role.colour.value else "No colour set"

        embed = discord.Embed(
            title=f"🎨 Role Preview — {role.name}",
            description=(
                f"The sidebar on the left shows the exact colour of this role.\n\n"
                f"🎨 Colour: `{hex_str}`\n"
                f"✨ Cost: **{shop_item['cost']:,}** dream points\n"
                f"📝 {shop_item.get('description', 'no description')}"
            ),
            color=colour,
        )
        embed.set_footer(text="Use /buy to purchase  •  Reverie  •  Hypnagogia")
        await interaction.response.send_message(embed=embed)

    # ── /buy ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="buy", description="Buy an item from the dream shop")
    @app_commands.describe(item="Name of the item to buy")
    async def buy(self, interaction: discord.Interaction, item: str):
        await interaction.response.defer(ephemeral=True)

        shop_item = await _get_item(self.items_col, interaction.guild_id, item)
        if not shop_item:
            await interaction.followup.send(
                f"*no item called **{item}** exists in the shop.* Check `/shop` for available items.",
                ephemeral=True,
            )
            return

        inventory = await _get_inventory(
            self.inv_col, interaction.user.id, interaction.guild_id
        )

        # Role removers are consumable — can own multiple, so skip duplicate check
        if shop_item["type"] != "role_remover":
            if any(i["name"].lower() == shop_item["name"].lower() for i in inventory):
                await interaction.followup.send(
                    f"*you already own **{shop_item['name']}**!* ✨",
                    ephemeral=True,
                )
                return

        # Check balance
        user_doc = await self.users_col.find_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        balance = user_doc.get("points", 0) if user_doc else 0
        if balance < shop_item["cost"]:
            shortage = shop_item["cost"] - balance
            await interaction.followup.send(
                f"*not enough dream points...* you need **{shortage:,}** more to buy **{shop_item['name']}**. 🌫️",
                ephemeral=True,
            )
            return

        # Deduct points
        await self.users_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {"$inc": {"points": -shop_item["cost"]}},
        )

        # Grant role if applicable
        if shop_item["type"] == "role":
            role = interaction.guild.get_role(shop_item["role_id"])
            if not role:
                await self.users_col.update_one(
                    {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
                    {"$inc": {"points": shop_item["cost"]}},
                )
                await interaction.followup.send(
                    "⚠️ That role no longer exists. Points have been refunded.",
                    ephemeral=True,
                )
                return
            try:
                await interaction.user.add_roles(role, reason="Reverie shop purchase")
            except discord.Forbidden:
                await self.users_col.update_one(
                    {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
                    {"$inc": {"points": shop_item["cost"]}},
                )
                await interaction.followup.send(
                    "⚠️ I don't have permission to assign that role. Points have been refunded — please let an admin know!",
                    ephemeral=True,
                )
                return

        # Add to inventory
        await self.inv_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
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
        if shop_item["type"] == "role_remover":
            embed.description += "\n\nUse `/removerole` to use it and remove one of your purchased roles."
        elif shop_item["type"] == "custom_title":
            embed.description += "\n\nUse `/setcustomtitle` to set your custom title. It will consume this item."
        embed.set_footer(text="Reverie  •  Hypnagogia")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /removerole ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="removerole",
        description="Use a role remover to remove one of your shop roles",
    )
    async def removerole(self, interaction: discord.Interaction):
        inventory = await _get_inventory(
            self.inv_col, interaction.user.id, interaction.guild_id
        )

        # Check they own at least one role remover
        remover = next((i for i in inventory if i["type"] == "role_remover"), None)
        if not remover:
            await interaction.response.send_message(
                "*you don't own a role remover.* Pick one up in the `/shop`! 🛒",
                ephemeral=True,
            )
            return

        # Find all shop role items and build role_id -> item name map
        all_shop_roles = await self.items_col.find(
            {"guild_id": interaction.guild_id, "type": "role"}
        ).to_list(length=100)

        shop_role_id_map = {item["role_id"]: item["name"] for item in all_shop_roles}

        # Show any shop role the member currently has, regardless of how they got it
        member = interaction.user
        member_role_ids = {r.id for r in member.roles}

        purchasable_roles = []
        role_item_names = {}

        for role_id, item_name in shop_role_id_map.items():
            if role_id in member_role_ids:
                role = interaction.guild.get_role(role_id)
                if role:
                    purchasable_roles.append(role)
                    role_item_names[role_id] = item_name

        if not purchasable_roles:
            await interaction.response.send_message(
                "*you don't have any shop roles to remove.*",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🗑️ Remove a Role",
            description="*choose which shop role to remove.*\n\nThis will consume one **Role Remover** from your inventory.",
            color=COLOUR_MAIN,
        )
        embed.set_footer(text="This menu expires in 60 seconds  •  Reverie")

        view = RoleRemoverView(
            purchasable_roles, self.inv_col, role_item_names, remover["name"]
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /inventory ────────────────────────────────────────────────────────────

    @app_commands.command(name="inventory", description="See the items you own")
    @app_commands.describe(member="Member to check (leave empty for yourself)")
    async def inventory(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        target = member or interaction.user
        inventory = await _get_inventory(self.inv_col, target.id, interaction.guild_id)

        embed = discord.Embed(
            title=f"🌙 {target.display_name}'s Inventory",
            color=COLOUR_MAIN,
        )

        if not inventory:
            embed.description = (
                "*nothing here yet — visit the `/shop` to spend your dream points!*"
            )
        else:
            active_title = await self._get_active_title(target.id, interaction.guild_id)
            lines = []
            for i in inventory:
                label = _item_type_label(i["type"])
                active = (
                    "  *(active)*"
                    if i["type"] == "title" and i["name"] == active_title
                    else ""
                )
                lines.append(f"{label}  **{i['name']}**{active}")
            embed.description = "\n".join(lines)
            if any(i["type"] == "title" for i in inventory):
                embed.set_footer(text="Use /settitle <n> to equip a title  •  Reverie")
            else:
                embed.set_footer(text="Reverie  •  Hypnagogia")

        await interaction.response.send_message(embed=embed)

    # ── /settitle ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="settitle",
        description="Equip one of your titles to display on your profile",
    )
    @app_commands.describe(
        title="Name of the title to equip (leave empty to remove your title)"
    )
    async def settitle(self, interaction: discord.Interaction, title: str = None):
        if title:
            inventory = await _get_inventory(
                self.inv_col, interaction.user.id, interaction.guild_id
            )
            owned_titles = [
                i["name"].lower() for i in inventory if i["type"] == "title"
            ]
            if title.lower() not in owned_titles:
                await interaction.response.send_message(
                    f"*you don't own a title called **{title}**.*  Check `/inventory` to see what you have.",
                    ephemeral=True,
                )
                return

        await self.inv_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {"$set": {"active_title": title}},
            upsert=True,
        )
        if title:
            await interaction.response.send_message(
                f"🌙 Title set to **{title}**. It will now show on your `/points` profile!",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "🌙 Title removed from your profile.",
                ephemeral=True,
            )

    # ── /additem (admin) ──────────────────────────────────────────────────────

    @app_commands.command(
        name="additem", description="[Admin] Add an item to the dream shop"
    )
    @app_commands.describe(
        name="Item name",
        cost="Point cost",
        item_type="Type of item",
        description="Short description shown in the shop",
        role="The role to grant (for role items only)",
    )
    @app_commands.choices(
        item_type=[
            app_commands.Choice(name="Role", value="role"),
            app_commands.Choice(name="Title", value="title"),
            app_commands.Choice(name="Role Remover", value="role_remover"),
            app_commands.Choice(name="Custom Title", value="custom_title"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    async def additem(
        self,
        interaction: discord.Interaction,
        name: str,
        cost: int,
        item_type: app_commands.Choice[str],
        description: str = "no description",
        role: discord.Role = None,
    ):
        if item_type.value == "role" and role is None:
            await interaction.response.send_message(
                "⚠️ You must provide a role when adding a role item.",
                ephemeral=True,
            )
            return

        existing = await _get_item(self.items_col, interaction.guild_id, name)
        if existing:
            await interaction.response.send_message(
                f"⚠️ An item called **{name}** already exists in the shop.",
                ephemeral=True,
            )
            return

        doc = {
            "guild_id": interaction.guild_id,
            "name": name,
            "type": item_type.value,
            "cost": cost,
            "description": description,
        }
        if item_type.value == "role":
            doc["role_id"] = role.id
            doc["role_colour"] = (
                f"{role.colour.value:06X}" if role.colour.value else None
            )

        await self.items_col.insert_one(doc)
        await interaction.response.send_message(
            f"✅ **{name}** added to the shop for **{cost:,}** dream points.",
            ephemeral=True,
        )

    # ── /removeitem (admin) ───────────────────────────────────────────────────

    @app_commands.command(
        name="removeitem", description="[Admin] Remove an item from the dream shop"
    )
    @app_commands.describe(name="Name of the item to remove")
    @app_commands.default_permissions(administrator=True)
    async def removeitem(self, interaction: discord.Interaction, name: str):
        result = await self.items_col.delete_one(
            {
                "guild_id": interaction.guild_id,
                "name": {"$regex": f"^{name}$", "$options": "i"},
            }
        )
        if result.deleted_count == 0:
            await interaction.response.send_message(
                f"⚠️ No item called **{name}** found in the shop.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"🌙 **{name}** has been removed from the shop.",
                ephemeral=True,
            )

    # ── /edititem (admin) ─────────────────────────────────────────────────────

    @app_commands.command(
        name="edititem", description="[Admin] Edit an existing item in the dream shop"
    )
    @app_commands.describe(
        name="Name of the item to edit",
        new_name="New name for the item (leave empty to keep current)",
        new_cost="New point cost (leave empty to keep current)",
        new_description="New description (leave empty to keep current)",
        new_role="New role to grant — role items only (leave empty to keep current)",
    )
    @app_commands.default_permissions(administrator=True)
    async def edititem(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str = None,
        new_cost: int = None,
        new_description: str = None,
        new_role: discord.Role = None,
    ):
        shop_item = await _get_item(self.items_col, interaction.guild_id, name)
        if not shop_item:
            await interaction.response.send_message(
                f"⚠️ No item called **{name}** found in the shop.",
                ephemeral=True,
            )
            return

        if not any([new_name, new_cost, new_description, new_role]):
            await interaction.response.send_message(
                "⚠️ You didn't provide anything to change! Supply at least one of: new_name, new_cost, new_description, new_role.",
                ephemeral=True,
            )
            return

        if new_role and shop_item["type"] != "role":
            await interaction.response.send_message(
                "⚠️ You can only update the role on a role-type item.",
                ephemeral=True,
            )
            return

        changes = {}
        summary = []
        if new_name:
            changes["name"] = new_name
            summary.append(f"name: **{name}** to **{new_name}**")
        if new_cost is not None:
            changes["cost"] = new_cost
            summary.append(f"cost: **{shop_item['cost']:,}** to **{new_cost:,}** pts")
        if new_description:
            changes["description"] = new_description
            summary.append("description updated")
        if new_role:
            changes["role_id"] = new_role.id
            summary.append(f"role updated to **{new_role.name}**")

        await self.items_col.update_one(
            {"guild_id": interaction.guild_id, "name": shop_item["name"]},
            {"$set": changes},
        )

        bullet_list = chr(10).join("• " + s for s in summary)
        await interaction.response.send_message(
            f"✅ **{name}** updated:{chr(10)}{bullet_list}",
            ephemeral=True,
        )

    # ── /setcustomtitle ───────────────────────────────────────────────────────

    @app_commands.command(
        name="setcustomtitle",
        description="Use a custom title item to set your own unique title",
    )
    @app_commands.describe(
        title="Your custom title (max 32 characters, letters/numbers/spaces only)"
    )
    async def setcustomtitle(self, interaction: discord.Interaction, title: str):
        # Validate length
        if len(title) > CUSTOM_TITLE_MAX_LEN:
            await interaction.response.send_message(
                f"⚠️ Title must be **{CUSTOM_TITLE_MAX_LEN} characters or fewer** (yours is {len(title)}).",
                ephemeral=True,
            )
            return

        # Validate characters
        if not CUSTOM_TITLE_PATTERN.match(title):
            await interaction.response.send_message(
                "⚠️ Title can only contain letters, numbers, spaces, and these characters: `' - ! ? .`",
                ephemeral=True,
            )
            return

        # Check they own a custom title item
        inventory = await _get_inventory(
            self.inv_col, interaction.user.id, interaction.guild_id
        )
        custom_item = next((i for i in inventory if i["type"] == "custom_title"), None)
        if not custom_item:
            await interaction.response.send_message(
                "*you don't own a Custom Title item.* Pick one up in the `/shop`! 🛒",
                ephemeral=True,
            )
            return

        # Consume the item and set the title
        await self.inv_col.update_one(
            {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
            {
                "$pull": {
                    "items": {"type": "custom_title", "name": custom_item["name"]}
                },
                "$set": {"active_title": title},
            },
        )

        embed = discord.Embed(
            title="🖊️ Custom Title Set",
            description=f"Your title is now **{title}**. It will show on your `/points` profile!",
            color=COLOUR_CONFIRM,
        )
        embed.set_footer(text="Buy another Custom Title item to change it  •  Reverie")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /equip ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="equip", description="Equip a purchased role from your inventory"
    )
    async def equip(self, interaction: discord.Interaction):
        inventory = await _get_inventory(
            self.inv_col, interaction.user.id, interaction.guild_id
        )
        owned_role_items = [i for i in inventory if i["type"] == "role"]

        if not owned_role_items:
            await interaction.response.send_message(
                "*you don't own any roles yet — visit the `/shop` to pick one up!*",
                ephemeral=True,
            )
            return

        # Fetch shop docs to get role_id
        shop_docs = []
        for inv_item in owned_role_items:
            doc = await self.items_col.find_one(
                {
                    "guild_id": interaction.guild_id,
                    "name": inv_item["name"],
                    "type": "role",
                }
            )
            if doc:
                shop_docs.append(doc)

        if not shop_docs:
            await interaction.response.send_message(
                "*couldn't find your roles in the shop. They may have been removed.*",
                ephemeral=True,
            )
            return

        options = [
            discord.SelectOption(
                label=doc["name"][:100],
                value=str(doc.get("role_id", 0)),
            )
            for doc in shop_docs[:25]
        ]

        select = discord.ui.Select(
            placeholder="Choose a role to equip...",
            options=options,
            min_values=1,
            max_values=1,
        )

        async def equip_callback(select_interaction: discord.Interaction):
            role = interaction.guild.get_role(int(select.values[0]))
            if not role:
                await select_interaction.response.edit_message(
                    content="⚠️ That role no longer exists. Contact an admin.", view=None
                )
                return
            try:
                await select_interaction.user.add_roles(role, reason="Equip role")
                await select_interaction.response.edit_message(
                    content=f"🌙 **{role.name}** equipped.", view=None
                )
            except discord.Forbidden:
                await select_interaction.response.edit_message(
                    content="⚠️ I don't have permission to assign that role.", view=None
                )

        select.callback = equip_callback
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "*choose a role to equip:*", view=view, ephemeral=True
        )

    # ── /unequip ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unequip", description="Unequip a role you currently have equipped"
    )
    async def unequip(self, interaction: discord.Interaction):
        inventory = await _get_inventory(
            self.inv_col, interaction.user.id, interaction.guild_id
        )
        owned_role_items = [i for i in inventory if i["type"] == "role"]

        if not owned_role_items:
            await interaction.response.send_message(
                "*you don't own any roles yet.*",
                ephemeral=True,
            )
            return

        # Only show roles they currently have equipped
        equipped = []
        for inv_item in owned_role_items:
            doc = await self.items_col.find_one(
                {
                    "guild_id": interaction.guild_id,
                    "name": inv_item["name"],
                    "type": "role",
                }
            )
            if not doc:
                continue
            role = interaction.guild.get_role(doc.get("role_id"))
            if role and role in interaction.user.roles:
                equipped.append((doc, role))

        if not equipped:
            await interaction.response.send_message(
                "*you don't have any roles equipped right now.*",
                ephemeral=True,
            )
            return

        options = [
            discord.SelectOption(
                label=role.name[:100],
                value=str(role.id),
            )
            for doc, role in equipped[:25]
        ]

        select = discord.ui.Select(
            placeholder="Choose a role to unequip...",
            options=options,
            min_values=1,
            max_values=1,
        )

        async def unequip_callback(select_interaction: discord.Interaction):
            role = interaction.guild.get_role(int(select.values[0]))
            if not role:
                await select_interaction.response.edit_message(
                    content="⚠️ That role no longer exists.", view=None
                )
                return
            try:
                await select_interaction.user.remove_roles(role, reason="Unequip role")
                await select_interaction.response.edit_message(
                    content=f"🌙 **{role.name}** unequipped.", view=None
                )
            except discord.Forbidden:
                await select_interaction.response.edit_message(
                    content="⚠️ I don't have permission to remove that role.", view=None
                )

        select.callback = unequip_callback
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "*choose a role to unequip:*", view=view, ephemeral=True
        )

    # ── Internal helper ───────────────────────────────────────────────────────

    async def _get_active_title(self, user_id: int, guild_id: int) -> str | None:
        doc = await self.inv_col.find_one({"user_id": user_id, "guild_id": guild_id})
        return doc.get("active_title") if doc else None


async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
