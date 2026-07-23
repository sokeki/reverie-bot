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
    "**Step 3:** Copy the *entire* URL from your browser's address bar and "
    "send it back here.\n\n"
    "-# Works with 2FA, Google, Facebook, Apple and all login methods. Tip: "
    "check 'Stay signed in' to avoid being signed out. Delete your message "
    "with the pasted URL afterwards, same as you would a password."
)


class ValShop(commands.Cog):
    """Personal Valorant daily shop, via each user's own Riot login (browser-redirect flow)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _do_dm_login_flow(
        self, user: discord.User, dm: discord.DMChannel
    ) -> riot_auth.AuthSuccess | None:
        """Sends the login link, waits for the user to paste back the
        resulting URL, and returns the parsed tokens. Returns None (having
        already told the user what happened) on timeout or a bad paste."""
        login_url = riot_auth.build_login_url()
        embed = discord.Embed(
            title="🔒 Login to your Riot Account",
            description=LOGIN_EMBED_DESCRIPTION.format(url=login_url),
            color=COLOUR_MAIN,
        )
        await dm.send(embed=embed)

        def check(m: discord.Message) -> bool:
            return m.author.id == user.id and m.channel.id == dm.id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await dm.send(
                "⚠️ Timed out waiting for the URL — run the command again when ready."
            )
            return None

        try:
            auth = riot_auth.redeem_redirect_url(msg.content)
        except riot_auth.AuthenticationError as e:
            await dm.send(f"⚠️ {e}")
            return None

        await dm.send("✅ Got it, logging you in...")
        return auth

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