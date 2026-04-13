"""
RR Tracker cog - polls the HenrikDev Valorant API every 2 minutes to detect
new competitive games and posts per-game updates. Posts a daily summary at
midnight UTC.

Required env vars:
  HENRIK_API_KEY  - your api.henrikdev.xyz key

Set channel with /setrrchannel
"""

import os
import aiohttp
from datetime import datetime, timezone, timedelta

import discord
from urllib.parse import quote
from discord import app_commands
from discord.ext import commands, tasks

from config import COLOUR_MAIN, COLOUR_LB

REGION = "eu"
PLATFORM = "pc"
API_BASE = "https://api.henrikdev.xyz"

TIER_COLOURS = {
    "Iron": 0x8D7F6B,
    "Bronze": 0x9E6B3E,
    "Silver": 0xA8B8C8,
    "Gold": 0xD4A843,
    "Platinum": 0x4DC8B4,
    "Diamond": 0x9F6FFF,
    "Ascendant": 0x4FBD6E,
    "Immortal": 0xC83250,
    "Radiant": 0xF8D76C,
}


def _tier_colour(tier_name: str) -> int:
    for name, colour in TIER_COLOURS.items():
        if tier_name.startswith(name):
            return colour
    return COLOUR_MAIN


def _rr_arrow(change: int) -> str:
    if change > 0:
        return f"▲ +{change}"
    elif change < 0:
        return f"▼ {change}"
    return f"- {change}"


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _winning_team(match: dict) -> str:
    teams = match.get("teams", {})
    # v3 format: list of dicts with team_id and won
    if isinstance(teams, list):
        for team in teams:
            if team.get("won"):
                return team["team_id"]
    # v2 format: dict with "red" and "blue" keys
    elif isinstance(teams, dict):
        for team_name, team_data in teams.items():
            if team_data.get("has_won"):
                return team_name
    return ""


class RRTracker(commands.Cog):
    """Tracks Valorant RR gains and losses for registered members."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.api_key = os.getenv("HENRIK_API_KEY", "")
        self.poll_task.start()
        self.daily_summary_task.start()

    def cog_unload(self):
        self.poll_task.cancel()
        self.daily_summary_task.cancel()
        if self.session:
            self.bot.loop.create_task(self.session.close())

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Authorization": self.api_key}
            )
        return self.session

    # ── API helpers ───────────────────────────────────────────────────────────

    async def _get_mmr(self, name: str, tag: str) -> dict | None:
        session = await self._get_session()
        url = (
            f"{API_BASE}/valorant/v3/mmr/{REGION}/{PLATFORM}/{quote(name)}/{quote(tag)}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data")
        except Exception:
            return None

    async def _get_matches(self, name: str, tag: str, count: int = 1) -> list:
        session = await self._get_session()
        url = f"{API_BASE}/valorant/v3/matches/{REGION}/{quote(name)}/{quote(tag)}?mode=competitive&size={count}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(
                        f"[RR Tracker] Matches API {resp.status} for {name}#{tag}: {text[:200]}"
                    )
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception as e:
            print(f"[RR Tracker] Matches request failed for {name}#{tag}: {e}")
            return []

    # ── /registerval ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="registerval", description="Link your Valorant account for RR tracking"
    )
    @app_commands.describe(username="Your Valorant username and tag, e.g. Name#EUW")
    async def registerval(self, interaction: discord.Interaction, username: str):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Please include your tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        await interaction.response.defer(ephemeral=True)

        mmr = await self._get_mmr(name, tag)
        if not mmr:
            await interaction.followup.send(
                f"⚠️ Couldn't find **{username}** on EU servers. Check the spelling and try again.",
                ephemeral=True,
            )
            return

        tier = mmr["current"]["tier"]["name"]
        rr = mmr["current"]["rr"]
        puuid = mmr["account"]["puuid"]

        await self.bot.val_accounts_col.update_one(
            {"discord_id": interaction.user.id, "guild_id": interaction.guild_id},
            {
                "$set": {
                    "discord_id": interaction.user.id,
                    "guild_id": interaction.guild_id,
                    "username": interaction.user.display_name,
                    "val_name": name,
                    "val_tag": tag,
                    "puuid": puuid,
                    "last_match_id": None,
                }
            },
            upsert=True,
        )

        await interaction.followup.send(
            f"✅ Linked **{name}#{tag}** - currently **{tier}** at **{rr} RR**. "
            f"Games will be tracked automatically!",
            ephemeral=True,
        )

    # ── /adminregisterval ─────────────────────────────────────────────────────

    @app_commands.command(
        name="adminregisterval",
        description="[Admin] Link a Valorant account to a Discord member",
    )
    @app_commands.describe(
        member="The Discord member to register",
        username="Their Valorant username and tag, e.g. Name#EUW",
    )
    @app_commands.default_permissions(administrator=True)
    async def adminregisterval(
        self, interaction: discord.Interaction, member: discord.Member, username: str
    ):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Please include the tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        await interaction.response.defer(ephemeral=True)

        mmr = await self._get_mmr(name, tag)
        if not mmr:
            await interaction.followup.send(
                f"⚠️ Couldn't find **{username}** on EU servers.", ephemeral=True
            )
            return

        tier = mmr["current"]["tier"]["name"]
        rr = mmr["current"]["rr"]
        puuid = mmr["account"]["puuid"]

        await self.bot.val_accounts_col.update_one(
            {"discord_id": member.id, "guild_id": interaction.guild_id},
            {
                "$set": {
                    "discord_id": member.id,
                    "guild_id": interaction.guild_id,
                    "username": member.display_name,
                    "val_name": name,
                    "val_tag": tag,
                    "puuid": puuid,
                    "last_match_id": None,
                }
            },
            upsert=True,
        )

        await interaction.followup.send(
            f"✅ Linked **{name}#{tag}** to **{member.display_name}** - currently **{tier}** at **{rr} RR**.",
            ephemeral=True,
        )

    # ── /setrrchannel ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="setrrchannel",
        description="[Admin] Set the channel for RR tracking updates",
    )
    @app_commands.describe(channel="Channel to post RR updates in")
    @app_commands.default_permissions(administrator=True)
    async def setrrchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"rr_channel_id": channel.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ RR updates will be posted in {channel.mention}.", ephemeral=True
        )

    # ── /unregisterval ────────────────────────────────────────────────────────

    @app_commands.command(
        name="unregisterval",
        description="Unlink your Valorant account from RR tracking",
    )
    async def unregisterval(self, interaction: discord.Interaction):
        result = await self.bot.val_accounts_col.delete_one(
            {"discord_id": interaction.user.id, "guild_id": interaction.guild_id}
        )
        if result.deleted_count:
            await interaction.response.send_message(
                "🌙 Your Valorant account has been unlinked.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "*you don't have a Valorant account linked.*", ephemeral=True
            )

    # ── /rrleaderboard ───────────────────────────────────────────────────────

    @app_commands.command(
        name="rrleaderboard",
        description="See the RR leaderboard for registered players",
    )
    async def rrleaderboard(self, interaction: discord.Interaction):
        accounts = await self.bot.val_accounts_col.find(
            {"guild_id": interaction.guild_id}
        ).to_list(length=100)

        if not accounts:
            await interaction.response.send_message(
                "*no Valorant accounts registered yet.*",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        rows = []
        for account in accounts:
            mmr = await self._get_mmr(account["val_name"], account["val_tag"])
            if not mmr:
                continue
            current = mmr["current"]
            rows.append(
                {
                    "name": f"{account['val_name']}#{account['val_tag']}",
                    "tier": current["tier"]["name"],
                    "rr": current["rr"],
                    "elo": current.get("elo", 0),
                    "change": current.get("last_change", 0),
                }
            )

        if not rows:
            await interaction.followup.send(
                "*couldn't fetch RR data right now.*", ephemeral=True
            )
            return

        rows.sort(key=lambda r: r["elo"], reverse=True)

        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = []
        for i, row in enumerate(rows):
            medal = medals.get(i, f"`#{i+1}`")
            change = f" `{_rr_arrow(row['change'])}`" if row["change"] != 0 else ""
            lines.append(
                f"{medal} **{row['name']}** - {row['tier']} {row['rr']} RR{change}"
            )

        embed = discord.Embed(
            title="🎯 RR Leaderboard",
            description="\n".join(lines),
            color=COLOUR_LB,
        )
        embed.set_footer(text=f"Reverie  -  {interaction.guild.name}")
        await interaction.followup.send(embed=embed)

    # ── /rrtrackertest ───────────────────────────────────────────────────────

    @app_commands.command(
        name="rrtrackertest", description="[Admin] Test the API for a specific account"
    )
    @app_commands.describe(username="Valorant username e.g. Name#TAG")
    @app_commands.default_permissions(administrator=True)
    async def rrtrackertest(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if "#" not in username:
            await interaction.followup.send(
                "⚠️ Include the tag e.g. `Name#EUW`", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        from urllib.parse import quote

        session = await self._get_session()
        url = f"{API_BASE}/valorant/v3/matches/{REGION}/{quote(name)}/{quote(tag)}?mode=competitive&size=1"
        try:
            async with session.get(url) as resp:
                status = resp.status
                text = await resp.text()
                await interaction.followup.send(
                    f"**URL:** `{url}`\n**Status:** {status}\n**Response:**\n```{text[:400]}```",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.followup.send(f"**Exception:** {e}", ephemeral=True)

    # ── /rrtrackerstatus ─────────────────────────────────────────────────────

    @app_commands.command(
        name="rrtrackerstatus", description="[Admin] Check if the RR tracker is running"
    )
    @app_commands.default_permissions(administrator=True)
    async def rrtrackerstatus(self, interaction: discord.Interaction):
        accounts = await self.bot.val_accounts_col.find(
            {"guild_id": interaction.guild_id}
        ).to_list(length=100)
        settings = await self.bot.settings_col.find_one(
            {"guild_id": interaction.guild_id}
        )
        channel_id = settings.get("rr_channel_id") if settings else None
        channel = interaction.guild.get_channel(channel_id) if channel_id else None

        lines = [
            f"**Poll task running:** {self.poll_task.is_running()}",
            f"**Next poll:** {self.poll_task.next_iteration.strftime('%H:%M:%S UTC') if self.poll_task.next_iteration else 'unknown'}",
            f"**RR channel:** {channel.mention if channel else '⚠️ not set - run /setrrchannel'}",
            f"**Registered accounts:** {len(accounts)}",
        ]
        if accounts:
            for a in accounts:
                lines.append(
                    f"- {a['val_name']}#{a['val_tag']} - last match ID: `{a.get('last_match_id') or 'null'}`"
                )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── Poll task ─────────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def poll_task(self):
        for guild in self.bot.guilds:
            settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
            if not settings or not settings.get("rr_channel_id"):
                continue
            channel = guild.get_channel(settings["rr_channel_id"])
            if not channel:
                continue
            accounts = await self.bot.val_accounts_col.find(
                {"guild_id": guild.id}
            ).to_list(length=100)
            for account in accounts:
                try:
                    await self._check_new_game(account, channel, guild)
                except Exception as e:
                    print(
                        f"[RR Tracker] Error checking {account.get('val_name')}#{account.get('val_tag')}: {e}"
                    )

    async def _check_new_game(
        self, account: dict, channel: discord.TextChannel, guild: discord.Guild
    ):
        name = account["val_name"]
        tag = account["val_tag"]

        matches = await self._get_matches(name, tag, count=1)
        if not matches:
            print(f"[RR Tracker] No matches returned for {name}#{tag}")
            return

        latest = matches[0]
        match_id = latest["metadata"].get("match_id") or latest["metadata"].get(
            "matchid"
        )
        last_id = account.get("last_match_id")

        if match_id == last_id:
            return

        # Update stored match ID first
        await self.bot.val_accounts_col.update_one(
            {"_id": account["_id"]},
            {"$set": {"last_match_id": match_id}},
        )

        # First time seeing this account - just store ID, no post
        if last_id is None:
            return

        # Find this player in the match - handle both v2 and v3 player list formats
        puuid = account.get("puuid", "")
        all_players = (
            latest["players"]
            if isinstance(latest["players"], list)
            else latest["players"].get("all_players", [])
        )
        player = next((p for p in all_players if p["puuid"] == puuid), None)
        if not player:
            return

        # Fetch current MMR for fresh RR + last_change
        mmr = await self._get_mmr(name, tag)
        if not mmr:
            return

        current = mmr["current"]
        tier_name = current["tier"]["name"]
        rr = current["rr"]
        rr_change = current.get("last_change", 0)
        won = (
            player.get("team_id") or player.get("team", "")
        ).lower() == _winning_team(latest).lower()

        kills = player["stats"]["kills"]
        deaths = player["stats"]["deaths"]
        assists = player["stats"]["assists"]
        agent = player.get("agent", {}).get("name") or player.get(
            "character", "Unknown"
        )
        map_name = latest["metadata"].get("map", {}).get("name") or latest[
            "metadata"
        ].get("map", "Unknown")

        # Match score - handle both v2 and v3 format
        player_team = (player.get("team_id") or player.get("team", "")).lower()
        rounds_won = 0
        rounds_lost = 0
        teams = latest.get("teams", {})
        if isinstance(teams, list):
            for team in teams:
                if team["team_id"].lower() == player_team:
                    rounds_won = team.get("rounds_won", 0)
                else:
                    rounds_lost = team.get("rounds_won", 0)
        elif isinstance(teams, dict):
            for team_name, team_data in teams.items():
                if team_name.lower() == player_team:
                    rounds_won = team_data.get("rounds_won", 0)
                else:
                    rounds_lost = team_data.get("rounds_won", 0)
        score_str = f"{rounds_won}-{rounds_lost}"
        result_str = "WIN" if won else "LOSS"

        embed = discord.Embed(
            title=f"{'🟢' if won else '🔴'}  {name}#{tag}  -  {result_str}",
            color=_tier_colour(tier_name),
        )
        embed.add_field(name="🏅 Rank", value=f"**{tier_name}**\n{rr} RR", inline=True)
        embed.add_field(
            name="📈 Change", value=f"**{_rr_arrow(rr_change)}**", inline=True
        )
        embed.add_field(
            name="⚔️ KDA", value=f"**{kills}/{deaths}/{assists}**", inline=True
        )
        embed.add_field(name="🗺️ Map", value=f"**{map_name}**", inline=True)
        embed.add_field(name="📊 Score", value=f"**{score_str}**", inline=True)
        embed.add_field(name="🧑‍✈️ Agent", value=f"**{agent}**", inline=True)
        embed.set_footer(text=f"Reverie  •  {guild.name}")

        await channel.send(embed=embed)

        # Store for daily summary
        await self.bot.val_games_col.insert_one(
            {
                "guild_id": guild.id,
                "discord_id": account["discord_id"],
                "val_name": name,
                "val_tag": tag,
                "date": _today_utc(),
                "match_id": match_id,
                "won": won,
                "rr_change": rr_change,
                "rr_after": rr,
                "tier": tier_name,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "agent": agent,
                "map": map_name,
            }
        )

    # ── Daily summary (midnight UTC) ──────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def daily_summary_task(self):
        now = datetime.now(timezone.utc)
        if now.hour != 0 or now.minute != 0:
            return
        for guild in self.bot.guilds:
            try:
                await self._post_daily_summary(guild)
            except Exception:
                pass

    async def _post_daily_summary(self, guild: discord.Guild):
        settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
        if not settings or not settings.get("rr_channel_id"):
            return
        channel = guild.get_channel(settings["rr_channel_id"])
        if not channel:
            return

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        games = await self.bot.val_games_col.find(
            {"guild_id": guild.id, "date": yesterday}
        ).to_list(length=500)

        if not games:
            return

        # Group by player
        by_player: dict[int, list] = {}
        for g in games:
            by_player.setdefault(g["discord_id"], []).append(g)

        lines = []
        for discord_id, player_games in sorted(
            by_player.items(),
            key=lambda kv: sum(g["rr_change"] for g in kv[1]),
            reverse=True,
        ):
            total_rr = sum(g["rr_change"] for g in player_games)
            wins = sum(1 for g in player_games if g["won"])
            losses = len(player_games) - wins
            win_rate = round(wins / len(player_games) * 100)
            end_rr = player_games[-1]["rr_after"]
            start_rr = end_rr - total_rr
            tier = player_games[-1]["tier"]
            name = player_games[0]["val_name"]
            tag = player_games[0]["val_tag"]
            rr_str = f"+{total_rr}" if total_rr >= 0 else str(total_rr)

            lines.append(
                f"**{name}#{tag}** : {rr_str}\n"
                f"{wins}W {losses}L ({win_rate}%) | {tier} {start_rr}rr → {tier} {end_rr}rr"
            )

        embed = discord.Embed(
            title="📊 Yesterday's Summary",
            description="\n\n".join(lines),
            color=COLOUR_LB,
        )
        embed.set_footer(text=f"{yesterday}  •  Reverie  •  {guild.name}")
        await channel.send(embed=embed)

    @poll_task.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    @daily_summary_task.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RRTracker(bot))
