import asyncio
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_MAIN, COLOUR_CONFIRM, FERNET_KEY
from utils import riot_auth

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None


def _get_fernet():
    """Returns a Fernet instance, or None if not configured. Never raises —
    a missing/bad key should degrade to a clear error message, not crash
    cog loading for the whole bot."""
    if Fernet is None or not FERNET_KEY:
        return None
    try:
        return Fernet(FERNET_KEY.encode())
    except Exception:
        return None


def _encrypt(data: dict):
    f = _get_fernet()
    if not f:
        return None
    return f.encrypt(json.dumps(data).encode())


def _decrypt(token) -> dict | None:
    f = _get_fernet()
    if not f or not token:
        return None
    try:
        return json.loads(f.decrypt(bytes(token)).decode())
    except Exception:
        return None


LOGIN_EMBED_DESCRIPTION = (
    "**Step 1:** Click the link below to log in:\n\n"
    "🔗 **[Click here to login]({url})**\n\n"
    "**Step 2:** After logging in, your browser will show an error page — "
    "this is normal!\n\n"
    "**Step 3:** Click the button below and paste the URL from your browser.\n\n"
    "-# Works with 2FA, Google, Facebook, Apple and all login methods. Tip: "
    "check 'Stay signed in' to avoid being signed out."
)


class RiotLoginModal(discord.ui.Modal, title="Paste your login URL"):
    url_input = discord.ui.TextInput(
        label="URL from your browser's address bar",
        style=discord.TextStyle.paragraph,
        placeholder="http://localhost/redirect#access_token=...",
        max_length=4000,
    )

    def __init__(self, on_submit_callback):
        super().__init__()
        self._on_submit_callback = on_submit_callback

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit_callback(interaction, self.url_input.value)


class RiotLoginView(discord.ui.View):
    def __init__(self, on_submit_callback, timeout: int = 300):
        super().__init__(timeout=timeout)
        self._on_submit_callback = on_submit_callback
        self.message: discord.Message | None = None

    @discord.ui.button(
        label="I've logged in — paste URL",
        style=discord.ButtonStyle.primary,
        emoji="📋",
    )
    async def paste_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(RiotLoginModal(self._on_submit_callback))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ValShop(commands.Cog):
    """Personal Valorant daily shop, via each user's own Riot login (browser-redirect flow)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _do_dm_login_flow(
        self, user: discord.User, dm: discord.DMChannel
    ) -> riot_auth.AuthSuccess | None:
        """Sends the login link + a button that opens a paste-URL form, waits
        for it to be submitted, and returns the parsed tokens. Returns None
        (having already told the user what happened) on timeout or a bad
        paste."""
        login_url = riot_auth.build_login_url()
        embed = discord.Embed(
            title="🔒 Login to your Riot Account",
            description=LOGIN_EMBED_DESCRIPTION.format(url=login_url),
            color=COLOUR_MAIN,
        )

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        async def handle_submit(modal_interaction: discord.Interaction, pasted_url: str):
            if not future.done():
                future.set_result(pasted_url)
            await modal_interaction.response.send_message(
                "✅ Got it, logging you in...", ephemeral=True
            )

        view = RiotLoginView(handle_submit)
        msg = await dm.send(embed=embed, view=view)
        view.message = msg

        try:
            pasted_url = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            await dm.send(
                "⚠️ Timed out waiting for the URL — run the command again when ready."
            )
            return None

        try:
            return riot_auth.redeem_redirect_url(pasted_url)
        except riot_auth.AuthenticationError as e:
            await dm.send(f"⚠️ {e}")
            return None

    # ── /linkriot ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="linkriot",
        description="Link your Riot account (via DM) to check your daily shop",
    )
    async def linkriot(self, interaction: discord.Interaction):
        if _get_fernet() is None:
            await interaction.response.send_message(
                "⚠️ This feature isn't configured yet (missing/invalid FERNET_KEY). "
                "Ask the bot owner to set it up.",
                ephemeral=True,
            )
            return

        try:
            dm = await interaction.user.create_dm()
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Couldn't open a DM with you — check your privacy settings.",
                ephemeral=True,
            )
            return

        if interaction.guild is not None:
            await interaction.response.send_message(
                "🌙 Check your DMs — let's get your account linked securely.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("🌙 Let's get you linked!")

        auth = await self._do_dm_login_flow(interaction.user, dm)
        if not auth:
            return

        try:
            puuid = await riot_auth.get_puuid(auth.access_token)
            shard = await riot_auth.get_region(auth.access_token, auth.id_token)
        except riot_auth.AuthenticationError as e:
            await dm.send(f"⚠️ {e}")
            return

        encrypted = _encrypt(
            {
                "access_token": auth.access_token,
                "id_token": auth.id_token,
                "expires_at": auth.expires_at,
            }
        )
        await self.bot.riot_login_col.update_one(
            {"user_id": interaction.user.id},
            {
                "$set": {
                    "user_id": interaction.user.id,
                    "puuid": puuid,
                    "shard": shard,
                    "session": encrypted,
                    "linked_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

        embed = discord.Embed(
            title="✅ Account linked",
            description=(
                "You can now use `/dailyshop`. Your login is only good for "
                "about an hour at a time — after that, running `/dailyshop` "
                "will just walk you through this same quick link + paste "
                "step again."
            ),
            color=COLOUR_CONFIRM,
        )
        await dm.send(embed=embed)

    # ── /unlinkriot ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="unlinkriot",
        description="Remove your linked Riot account from this bot",
    )
    async def unlinkriot(self, interaction: discord.Interaction):
        result = await self.bot.riot_login_col.delete_one(
            {"user_id": interaction.user.id}
        )
        if result.deleted_count:
            await interaction.response.send_message(
                "✅ Your Riot account has been unlinked.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You don't have a linked account.", ephemeral=True
            )

    # ── /dailyshop ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="dailyshop",
        description="See your (or a linked friend's) daily Valorant shop",
    )
    @app_commands.describe(member="Whose shop to check (must have linked their own account)")
    async def dailyshop(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        target = member or interaction.user

        doc = await self.bot.riot_login_col.find_one({"user_id": target.id})
        if not doc:
            who = "You haven't" if target.id == interaction.user.id else f"**{target.display_name}** hasn't"
            await interaction.response.send_message(
                f"⚠️ {who} linked a Riot account yet. Use `/linkriot` first.",
                ephemeral=True,
            )
            return

        session = _decrypt(doc.get("session"))
        if not session:
            await interaction.response.send_message(
                "⚠️ Couldn't decrypt the stored session (bot config may have changed). "
                "Please `/linkriot` again.",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc).timestamp()
        needs_relogin = session.get("expires_at", 0) - 60 <= now

        if needs_relogin and target.id != interaction.user.id:
            await interaction.response.send_message(
                f"⚠️ **{target.display_name}**'s login has expired. "
                f"They'll need to run `/linkriot` again themselves.",
                ephemeral=True,
            )
            return

        if needs_relogin:
            await interaction.response.send_message(
                "🌙 Your login has expired — check your DMs to relink, "
                "then I'll post your shop here once you're done.",
                ephemeral=True,
            )
            try:
                dm = await interaction.user.create_dm()
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ Couldn't DM you — check your privacy settings.",
                    ephemeral=True,
                )
                return
            auth = await self._do_dm_login_flow(interaction.user, dm)
            if not auth:
                return
            await self.bot.riot_login_col.update_one(
                {"user_id": target.id},
                {
                    "$set": {
                        "session": _encrypt(
                            {
                                "access_token": auth.access_token,
                                "id_token": auth.id_token,
                                "expires_at": auth.expires_at,
                            }
                        )
                    }
                },
            )
        else:
            await interaction.response.defer()
            auth = riot_auth.AuthSuccess(
                session["access_token"], session["id_token"], session["expires_at"]
            )

        try:
            entitlement = await riot_auth.get_entitlement(auth.access_token)
            storefront = await riot_auth.get_storefront(
                auth.access_token, entitlement, doc["puuid"], doc["shard"]
            )
        except riot_auth.AuthenticationError as e:
            await interaction.followup.send(f"⚠️ {e}")
            return

        panel = storefront.get("SkinsPanelLayout", {})
        offer_ids = panel.get("SingleItemOffers", [])
        remaining_seconds = panel.get("SingleItemOffersRemainingDurationInSeconds", 0)

        if not offer_ids:
            await interaction.followup.send("⚠️ Couldn't read the shop data.")
            return

        skins = await asyncio.gather(
            *(self._get_skin_info(uuid) for uuid in offer_ids)
        )

        hours = remaining_seconds // 3600
        minutes = (remaining_seconds % 3600) // 60

        embeds = []
        header = discord.Embed(
            title=f"🌙 {target.display_name}'s daily shop",
            description=f"Resets in **{hours}h {minutes}m**",
            color=COLOUR_MAIN,
        )
        header.set_thumbnail(url=target.display_avatar.url)
        embeds.append(header)

        for name, icon in skins:
            e = discord.Embed(title=name or "Unknown skin", color=COLOUR_MAIN)
            if icon:
                e.set_image(url=icon)
            embeds.append(e)

        await interaction.followup.send(embeds=embeds)

    async def _get_skin_info(self, skin_uuid: str) -> tuple[str, str]:
        import aiohttp

        url = f"https://valorant-api.com/v1/weapons/skinlevels/{skin_uuid}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as r:
                    if r.status != 200:
                        return ("Unknown skin", "")
                    data = await r.json()
            info = data.get("data", {})
            return (info.get("displayName", "Unknown skin"), info.get("displayIcon", ""))
        except Exception:
            return ("Unknown skin", "")


async def setup(bot: commands.Bot):
    await bot.add_cog(ValShop(bot))