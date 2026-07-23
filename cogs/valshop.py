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


class ValShop(commands.Cog):
    """Personal Valorant daily shop, via each user's own Riot login (DM-only)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        if interaction.guild is not None:
            await interaction.response.send_message(
                "🌙 Check your DMs — let's get your account linked securely.",
                ephemeral=True,
            )

        try:
            dm = await interaction.user.create_dm()
        except discord.Forbidden:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "⚠️ Couldn't open a DM with you.", ephemeral=True
                )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "🌙 Let's get your account linked securely, right here in DMs.",
            )

        def check(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == dm.id

        try:
            await dm.send(
                "**Linking your Riot account**\n\n"
                "This is only ever done here in DMs — never type your password "
                "into a server channel or slash command.\n\n"
                "First, what's the **username or email** you use to log into the "
                "Riot Client? (Not your Riot ID/tag — the actual login name.)"
            )
            username_msg = await self.bot.wait_for(
                "message", check=check, timeout=120
            )
            username = username_msg.content.strip()

            await dm.send(
                "Got it. Now send your **password**.\n"
                "-# I can't delete this message for you in DMs — please delete it "
                "yourself right after sending, as good practice."
            )
            password_msg = await self.bot.wait_for(
                "message", check=check, timeout=120
            )
            password = password_msg.content.strip()

            await dm.send("Logging in with Riot...")

            try:
                auth = await riot_auth.authorize(username, password)
            except riot_auth.MFARequired as mfa:
                hint = f" (sent to {mfa.email_hint})" if mfa.email_hint else ""
                await dm.send(
                    f"Riot sent a verification code{hint}. What's the code?"
                )
                code_msg = await self.bot.wait_for(
                    "message", check=check, timeout=180
                )
                code = code_msg.content.strip()
                auth = await riot_auth.submit_mfa(mfa.cookies, code)
            finally:
                # Discard credentials from memory as soon as we're done with them
                username = password = None

            puuid = await riot_auth.get_puuid(auth.access_token)
            shard = await riot_auth.get_region(auth.access_token, auth.id_token)

            encrypted = _encrypt({"cookies": auth.cookies})
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
                    "You can now use `/dailyshop` to see your shop. "
                    "You won't need to log in again unless your session expires "
                    "(usually after a few weeks), in which case just run "
                    "`/linkriot` again."
                ),
                color=COLOUR_CONFIRM,
            )
            await dm.send(embed=embed)

        except asyncio.TimeoutError:
            await dm.send("⚠️ Timed out waiting for a reply — run `/linkriot` again when ready.")
        except riot_auth.AuthenticationError as e:
            await dm.send(f"⚠️ {e}")
        except Exception as e:
            print(f"[ValShop] linkriot failed for {interaction.user}: {e!r}")
            await dm.send(
                "⚠️ Something went wrong linking your account. "
                "The bot owner has been notified via console logs."
            )

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

        await interaction.response.defer()

        try:
            auth = await riot_auth.reauth_with_cookies(session["cookies"])
        except riot_auth.AuthenticationError:
            await self.bot.riot_login_col.delete_one({"user_id": target.id})
            who = "Your" if target.id == interaction.user.id else f"{target.display_name}'s"
            await interaction.followup.send(
                f"⚠️ {who} linked session has expired. Please `/linkriot` again."
            )
            return

        # Refresh stored cookies — Riot may rotate them on each reauth
        await self.bot.riot_login_col.update_one(
            {"user_id": target.id},
            {"$set": {"session": _encrypt({"cookies": auth.cookies})}},
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