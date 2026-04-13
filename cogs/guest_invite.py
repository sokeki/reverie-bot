import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from config import COLOUR_CONFIRM


async def _get_settings(settings_col, guild_id: int) -> dict:
    doc = await settings_col.find_one({"guild_id": guild_id})
    return doc or {}


class GuestInvite(commands.Cog):
    """Guest invite system - generates a one-use invite that drags guests into VC."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # invite code -> metadata
        self.pending_invites: dict[str, dict] = {}
        # user_id -> target vc_id (waiting for them to join a VC)
        self.pending_guests: dict[int, dict] = {}

    # ── /setinviterole (admin) ────────────────────────────────────────────────

    @app_commands.command(
        name="setinviterole",
        description="[Admin] Set the role required to generate guest invites",
    )
    @app_commands.describe(role="The role that can generate guest invites")
    @app_commands.default_permissions(administrator=True)
    async def setinviterole(self, interaction: discord.Interaction, role: discord.Role):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"invite_role_id": role.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ **{role.name}** can now generate guest invites with `/guestinvite`.",
            ephemeral=True,
        )

    # ── /setlingeringrole (admin) ────────────────────────────────────────────

    @app_commands.command(
        name="setlingeringrole",
        description="[Admin] Set the role that members must have to be dragged with /drag",
    )
    @app_commands.describe(role="The role that can be dragged into a VC")
    @app_commands.default_permissions(administrator=True)
    async def setlingeringrole(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"lingering_role_id": role.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Members with **{role.name}** can now be dragged with `/drag`.",
            ephemeral=True,
        )

    # ── /guestinvite ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="guestinvite",
        description="Generate a one-use guest invite - guest is moved to your VC when they join one",
    )
    async def guestinvite(self, interaction: discord.Interaction):
        settings = await _get_settings(self.bot.settings_col, interaction.guild_id)

        # Check invite role is configured
        invite_role_id = settings.get("invite_role_id")
        if not invite_role_id:
            await interaction.response.send_message(
                "⚠️ No invite role has been set. Ask an admin to run `/setinviterole` first.",
                ephemeral=True,
            )
            return

        # Check the inviter has the required role
        if invite_role_id not in [r.id for r in interaction.user.roles]:
            role = interaction.guild.get_role(invite_role_id)
            await interaction.response.send_message(
                f"⚠️ You need the **{role.name if role else 'required'}** role to generate guest invites.",
                ephemeral=True,
            )
            return

        # Determine target VC - only use inviter's current VC if they're in one
        if interaction.user.voice and interaction.user.voice.channel:
            vc = interaction.user.voice.channel
            vc_note = f"your current VC - **{vc.name}**"
            vc_id = vc.id
            vc_name = vc.name
        else:
            vc_id = None
            vc_name = None
            vc_note = "a voice channel once they join one (you weren't in a VC when you generated this)"

        # Find a text channel to attach the invite to
        invite_channel = (
            interaction.channel
            if isinstance(interaction.channel, discord.TextChannel)
            else next(
                (
                    c
                    for c in interaction.guild.text_channels
                    if c.permissions_for(interaction.guild.me).create_instant_invite
                ),
                None,
            )
        )
        if not invite_channel:
            await interaction.response.send_message(
                "⚠️ Couldn't find a channel to create an invite for.",
                ephemeral=True,
            )
            return

        try:
            invite = await invite_channel.create_invite(
                max_uses=1,
                max_age=600,
                unique=True,
                reason=f"Guest invite by {interaction.user.display_name}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I don't have permission to create invites. Please check my permissions.",
                ephemeral=True,
            )
            return

        self.pending_invites[invite.code] = {
            "inviter_id": interaction.user.id,
            "guild_id": interaction.guild_id,
            "vc_id": vc_id,
            "vc_name": vc_name,
            "expires_at": datetime.now(timezone.utc).timestamp() + 600,
        }

        embed = discord.Embed(
            title="🌙 Guest Invite Created",
            description=(
                f"Share this link with your guest:\n"
                f"## {invite.url}\n"
                f"Once they join the server and hop into any voice channel, "
                f"they will automatically be moved to {vc_note}.\n\n"
                f"-# *Expires in 10 minutes · 1 use · guest is kicked when they leave the VC · "
                f"kicked after 1 hour if they never join a VC*"
            ),
            color=COLOUR_CONFIRM,
        )
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")

        try:
            await interaction.user.send(embed=embed)
            # Public confirmation in channel, private invite stays in DMs
            await interaction.response.send_message(
                f"🌙 **{interaction.user.display_name}** is inviting a guest - invite sent to their DMs!",
            )
        except discord.Forbidden:
            # DMs closed - send invite ephemerally so the link stays private
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── on_member_join - tag guest and start 1-hour kick timer ───────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        now = datetime.now(timezone.utc).timestamp()

        try:
            current_invites = await guild.invites()
        except discord.Forbidden:
            return

        # Find which pending invite was used
        used_code = None
        for code, data in list(self.pending_invites.items()):
            if data["guild_id"] != guild.id:
                continue
            still_exists = any(i.code == code for i in current_invites)
            if not still_exists or data.get("expires_at", 0) < now:
                used_code = code
                break

        if not used_code:
            return

        invite_data = self.pending_invites.pop(used_code)

        # Mark as pending guest - move them when they join a VC
        # vc_id may be None if inviter wasn't in a VC
        self.pending_guests[member.id] = {
            "guild_id": guild.id,
            "vc_id": invite_data["vc_id"],
        }

        # Tag in DB for kick-on-leave tracking
        await self.bot.settings_col.update_one(
            {"guild_id": guild.id},
            {"$addToSet": {"guests": {"user_id": member.id}}},
            upsert=True,
        )

        # Start 1-hour timer - kick if they never join a VC
        self.bot.loop.create_task(self._kick_if_idle(member, guild))

    async def _kick_if_idle(self, member: discord.Member, guild: discord.Guild):
        """Kick a guest if they haven't joined a VC within 1 hour."""
        await asyncio.sleep(3600)  # 1 hour

        # If they're still in pending_guests they never joined a VC
        if member.id not in self.pending_guests:
            return

        self.pending_guests.pop(member.id, None)

        # Remove from guest list in DB
        await self.bot.settings_col.update_one(
            {"guild_id": guild.id},
            {"$pull": {"guests": {"user_id": member.id}}},
        )

        try:
            await member.kick(reason="Guest never joined a voice channel within 1 hour")
        except (discord.Forbidden, discord.NotFound):
            pass

    # ── on_voice_state_update - move pending guest, kick on leave ─────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        joined = before.channel is None and after.channel is not None
        left_vc = before.channel is not None and after.channel is None

        # Pending guest just joined a VC - move them to target if one was set
        if joined and member.id in self.pending_guests:
            guest_data = self.pending_guests.pop(member.id)
            target_vc_id = guest_data.get("vc_id")
            if target_vc_id:
                target_vc = guild.get_channel(target_vc_id)
                if target_vc and isinstance(target_vc, discord.VoiceChannel):
                    if after.channel and after.channel.id != target_vc.id:
                        try:
                            await member.move_to(
                                target_vc, reason="Guest invite - moved to inviter's VC"
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            pass
            # If vc_id is None the inviter wasn't in a VC - guest stays wherever they joined

        # Tagged guest left VC entirely - kick them
        if left_vc:
            doc = await self.bot.settings_col.find_one({"guild_id": guild.id})
            if not doc:
                return
            guest = next(
                (g for g in doc.get("guests", []) if g["user_id"] == member.id), None
            )
            if not guest:
                return

            await self.bot.settings_col.update_one(
                {"guild_id": guild.id},
                {"$pull": {"guests": {"user_id": member.id}}},
            )
            try:
                await member.kick(
                    reason="Guest left the voice channel - temporary membership ended"
                )
            except discord.Forbidden:
                pass

    # ── /drag ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="drag",
        description="Drag a member into your current voice channel",
    )
    @app_commands.describe(member="The member to drag into your VC")
    async def drag(self, interaction: discord.Interaction, member: discord.Member):
        settings = await _get_settings(self.bot.settings_col, interaction.guild_id)

        # Check invite role is configured
        invite_role_id = settings.get("invite_role_id")
        if not invite_role_id:
            await interaction.response.send_message(
                "⚠️ No invite role has been set. Ask an admin to run `/setinviterole` first.",
                ephemeral=True,
            )
            return

        # Check the caller has the required role
        if invite_role_id not in [r.id for r in interaction.user.roles]:
            role = interaction.guild.get_role(invite_role_id)
            await interaction.response.send_message(
                f"⚠️ You need the **{role.name if role else 'required'}** role to use `/drag`.",
                ephemeral=True,
            )
            return

        # Check the caller is in a VC
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ You need to be in a voice channel to drag someone.",
                ephemeral=True,
            )
            return

        # Check the lingering role is configured
        lingering_role_id = settings.get("lingering_role_id")
        if not lingering_role_id:
            await interaction.response.send_message(
                "⚠️ No lingering role has been set. Ask an admin to run `/setlingeringrole` first.",
                ephemeral=True,
            )
            return

        # Check the target has the lingering role
        if lingering_role_id not in [r.id for r in member.roles]:
            lingering_role = interaction.guild.get_role(lingering_role_id)
            await interaction.response.send_message(
                f"⚠️ **{member.display_name}** doesn't have the **{lingering_role.name if lingering_role else 'lingering'}** role and can't be dragged.",
                ephemeral=True,
            )
            return

        # Check the target is in a VC
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                f"⚠️ **{member.display_name}** is not in a voice channel.",
                ephemeral=True,
            )
            return

        # Don't drag them if they're already in the same VC
        target_vc = interaction.user.voice.channel
        if member.voice.channel.id == target_vc.id:
            await interaction.response.send_message(
                f"⚠️ **{member.display_name}** is already in **{target_vc.name}**.",
                ephemeral=True,
            )
            return

        try:
            await member.move_to(
                target_vc, reason=f"Dragged by {interaction.user.display_name}"
            )
            await interaction.response.send_message(
                f"🌙 **{member.display_name}** has been dragged to **{target_vc.name}** by {interaction.user.mention}.",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I don't have permission to move that member.",
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "⚠️ Something went wrong trying to move them.",
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(GuestInvite(bot))
