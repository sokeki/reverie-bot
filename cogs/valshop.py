import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOUR_MAIN
from utils import riot_auth
from utils.crypto import encrypt_session, decrypt_session, is_configured

COOKIE_WALKTHROUGH_DESCRIPTION = (
    "This is more technical, only use it if you're comfortable with browser "
    "DevTools.\n\n"
    "**Step 1:** Go to <https://account.riotgames.com> and make sure you're logged "
    "in (check 'Remember me').\n"
    "**Step 2:** Open a new tab, press F12 (or Ctrl+Shift+I) to open DevTools, and "
    "go to the **Network** tab.\n"
    "**Step 3:** With DevTools still open, visit <https://auth.riotgames.com/> — "
    "you'll see \"An error occurred!\", that's expected, ignore it.\n"
    "**Step 4:** In the Network tab, find the request called `auth.riotgames.com`, "
    "click it, scroll to **Request Headers**, and find the **cookie** field.\n"
    "**Step 5:** Pick one:\n"
    "  • **Quick (~1 week session):** find **`ssid=`** inside the cookie field and "
    "copy just that piece, from `ssid=` to the next semicolon (or the end, if it's "
    "last). Paste it in the first box below.\n"
    "  • **Longer (~3 week session):** copy the *entire* cookie field. If it's too "
    "long for one box, split it roughly in half, ideally right at a `;` between two "
    "cookies, paste the first half in the first box, the second half in the second "
    "box, with no extra spaces added between them.\n"
    "**Step 6:** Click the button below and paste using whichever method you picked.\n\n"
    "-# Works in Chrome/Edge/Opera. Firefox truncates long cookies and won't work here."
)


class CookiePasteModal(discord.ui.Modal, title="Paste your Riot cookie"):
    cookie_part1 = discord.ui.TextInput(
        label="Cookie value (or first half, if it's long)",
        style=discord.TextStyle.paragraph,
        placeholder="ssid=eyJhbGci... (or the full cookie header)",
        max_length=4000,
    )
    cookie_part2 = discord.ui.TextInput(
        label="Second half (only if you had to split it)",
        style=discord.TextStyle.paragraph,
        placeholder="Leave blank if everything fit in the box above",
        required=False,
        max_length=4000,
    )

    def __init__(self, on_submit_callback):
        super().__init__()
        self._on_submit_callback = on_submit_callback

    async def on_submit(self, interaction: discord.Interaction):
        combined = self.cookie_part1.value + self.cookie_part2.value
        await self._on_submit_callback(interaction, combined)


class CookiePasteView(discord.ui.View):
    def __init__(self, on_submit_callback, timeout: int = 600):
        super().__init__(timeout=timeout)
        self._on_submit_callback = on_submit_callback

    @discord.ui.button(
        label="I've copied my cookie — paste it",
        style=discord.ButtonStyle.primary,
        emoji="🍪",
    )
    async def paste_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(CookiePasteModal(self._on_submit_callback))


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
    def __init__(self, cog: "ValShop", on_submit_callback, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
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
        await interaction.response.send_modal(
            RiotLoginModal(self._on_submit_callback)
        )

    @discord.ui.button(
        label="Advanced: use browser cookies (lasts longer)",
        style=discord.ButtonStyle.secondary,
        emoji="🍪",
    )
    async def advanced_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        async def handle_submit(modal_interaction: discord.Interaction, pasted: str):
            await self.cog._handle_cookie_submit(modal_interaction, pasted)

        embed = discord.Embed(
            title="🍪 Advanced: link via browser cookies",
            description=COOKIE_WALKTHROUGH_DESCRIPTION,
            color=COLOUR_MAIN,
        )
        await interaction.response.send_message(
            embed=embed, view=CookiePasteView(handle_submit)
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ValShop(commands.Cog):
    """Personal Valorant daily shop.

    Two ways to link an account:
      - Default: click a real Riot login link, log in normally in your own
        browser, it's auto-captured via the dashboard. Zero technical steps,
        but sessions only last ~1 hour (that's all a login-redirect can ever
        produce — there's no cookie in it).
      - Advanced: manually copy a session cookie via browser DevTools.
        More technical, but sessions last 1-3 weeks.
    Either way, your actual password is never seen or stored — only ever
    typed into Riot's own real login page.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _store_session(
        self, user_id: int, auth: riot_auth.AuthSuccess, puuid: str, shard: str
    ):
        encrypted = encrypt_session({"cookies": auth.cookies})
        await self.bot.riot_login_col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "puuid": puuid,
                    "shard": shard,
                    "session": encrypted,
                    "linked_at": datetime.now(timezone.utc),
                },
                "$unset": {"pending_state": ""},
            },
            upsert=True,
        )

    async def _handle_cookie_submit(
        self, modal_interaction: discord.Interaction, pasted: str
    ):
        await modal_interaction.response.send_message(
            "Logging in with your cookie...", ephemeral=True
        )
        try:
            cookies = riot_auth.parse_cookie_string(pasted)
            auth = await riot_auth.reauth_with_cookies(cookies)
        except riot_auth.AuthenticationError as e:
            await modal_interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        try:
            puuid = await riot_auth.get_puuid(auth.access_token)
            shard = await riot_auth.get_region(auth.access_token, auth.id_token)
        except riot_auth.AuthenticationError as e:
            await modal_interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        await self._store_session(modal_interaction.user.id, auth, puuid, shard)
        await modal_interaction.followup.send(
            "✅ Linked via cookies! This should last 1-3 weeks before you need to relink.",
            ephemeral=True,
        )

    async def _do_login_flow(
        self, user: discord.User, dm: discord.DMChannel
    ) -> bool:
        """Sends the login link + paste-URL button (and the advanced cookie
        option), waits for a submission, and stores whichever type of
        session results. Returns True on success, having already messaged
        the user either way."""
        login_url = riot_auth.build_login_url()
        embed = discord.Embed(
            title="🔒 Login to your Riot Account",
            description=(
                f"**Click the link below to log in:**\n\n"
                f"🔗 **[Click here to login]({login_url})**\n\n"
                f"After logging in, your browser will show an error page — "
                f"this is normal! Copy the *entire* URL from your address bar "
                f"and click the button below to paste it in.\n\n"
                f"-# Session lasts about an hour this way. Want it to last "
                f"1-3 weeks instead? Use the advanced button below."
            ),
            color=COLOUR_MAIN,
        )

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        async def handle_url_submit(modal_interaction: discord.Interaction, pasted_url: str):
            if not future.done():
                future.set_result(pasted_url)
            await modal_interaction.response.send_message(
                "✅ Got it, logging you in...", ephemeral=True
            )

        view = RiotLoginView(self, handle_url_submit)
        msg = await dm.send(embed=embed, view=view)
        view.message = msg

        try:
            pasted_url = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            await dm.send(
                "⚠️ Timed out waiting for the URL — run the command again when ready."
            )
            return False

        try:
            auth = riot_auth.redeem_redirect_url(pasted_url)
        except riot_auth.AuthenticationError as e:
            await dm.send(f"⚠️ {e}")
            return False

        try:
            puuid = await riot_auth.get_puuid(auth.access_token)
            shard = await riot_auth.get_region(auth.access_token, auth.id_token)
        except riot_auth.AuthenticationError as e:
            await dm.send(f"⚠️ {e}")
            return False

        encrypted = encrypt_session(
            {
                "access_token": auth.access_token,
                "id_token": auth.id_token,
                "expires_at": auth.expires_at,
            }
        )
        await self.bot.riot_login_col.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "user_id": user.id,
                    "puuid": puuid,
                    "shard": shard,
                    "session": encrypted,
                    "linked_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        await dm.send("✅ You're linked!")
        return True

    # ── /linkriot ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="linkriot",
        description="Link your Riot account to check your daily shop",
    )
    async def linkriot(self, interaction: discord.Interaction):
        if not is_configured():
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
                "🌙 Check your DMs — let's get your account linked.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("🌙 Let's get you linked!")

        await self._do_login_flow(interaction.user, dm)

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
        if not doc or not doc.get("session"):
            who = "You haven't" if target.id == interaction.user.id else f"**{target.display_name}** hasn't"
            await interaction.response.send_message(
                f"⚠️ {who} linked a Riot account yet. Use `/linkriot` first.",
                ephemeral=True,
            )
            return

        session = decrypt_session(doc.get("session"))
        if not session:
            await interaction.response.send_message(
                "⚠️ Couldn't decrypt the stored session (bot config may have changed). "
                "Please `/linkriot` again.",
                ephemeral=True,
            )
            return

        auth = None
        puuid, shard = doc.get("puuid"), doc.get("shard")

        if "cookies" in session:
            # Advanced (DevTools) path — silent reauth via the stored cookie
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
            await self.bot.riot_login_col.update_one(
                {"user_id": target.id},
                {"$set": {"session": encrypt_session({"cookies": auth.cookies})}},
            )
        else:
            # Default (dashboard) path — short-lived token, no silent refresh
            now = datetime.now(timezone.utc).timestamp()
            still_valid = session.get("expires_at", 0) - 60 > now

            if still_valid:
                await interaction.response.defer()
                auth = riot_auth.AuthSuccess(
                    session["access_token"], session["id_token"], {},
                    session["expires_at"],
                )
            elif target.id != interaction.user.id:
                await interaction.response.send_message(
                    f"⚠️ **{target.display_name}**'s login has expired. "
                    f"They'll need to run `/linkriot` again themselves.",
                    ephemeral=True,
                )
                return
            else:
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
                success = await self._do_login_flow(interaction.user, dm)
                if not success:
                    return
                fresh_doc = await self.bot.riot_login_col.find_one(
                    {"user_id": target.id}
                )
                fresh_session = decrypt_session(fresh_doc.get("session"))
                if not fresh_session:
                    await interaction.followup.send("⚠️ Something went wrong after linking.")
                    return
                puuid, shard = fresh_doc.get("puuid"), fresh_doc.get("shard")
                if "cookies" in fresh_session:
                    auth = await riot_auth.reauth_with_cookies(fresh_session["cookies"])
                else:
                    auth = riot_auth.AuthSuccess(
                        fresh_session["access_token"], fresh_session["id_token"], {},
                        fresh_session["expires_at"],
                    )

        try:
            entitlement = await riot_auth.get_entitlement(auth.access_token)
            storefront = await riot_auth.get_storefront(
                auth.access_token, entitlement, puuid, shard
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