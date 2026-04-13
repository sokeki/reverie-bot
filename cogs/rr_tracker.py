"""
RR Tracker cog - polls the HenrikDev Valorant API every 2 minutes to detect
new competitive games and posts per-game updates. Posts a daily summary at
midnight UTC.

Required env vars:
  HENRIK_API_KEY  - your api.henrikdev.xyz key

Set channel with /setrrchannel
"""

import os
import asyncio
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


# Caches for agent and map lookups
_agent_uuid_cache: dict[str, str] = {}
_map_image_cache: dict[str, str] = {}


async def _get_agent_icon(
    session: aiohttp.ClientSession, agent_name: str
) -> str | None:
    """Return the display icon URL for an agent by name."""
    global _agent_uuid_cache
    if not _agent_uuid_cache:
        try:
            async with session.get(
                "https://valorant-api.com/v1/agents?isPlayableCharacter=true"
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for a in data.get("data", []):
                        _agent_uuid_cache[a["displayName"].lower()] = a["uuid"]
        except Exception:
            pass
    uuid = _agent_uuid_cache.get(agent_name.lower())
    if uuid:
        return f"https://media.valorant-api.com/agents/{uuid}/displayicon.png"
    return None


async def _get_map_image(session: aiohttp.ClientSession, map_name: str) -> str | None:
    """Return the splash image URL for a map by name."""
    global _map_image_cache
    if not _map_image_cache:
        try:
            async with session.get("https://valorant-api.com/v1/maps") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("data", []):
                        _map_image_cache[m["displayName"].lower()] = m.get("splash", "")
        except Exception:
            pass
    return _map_image_cache.get(map_name.lower())


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
    if isinstance(teams, list):
        for team in teams:
            if isinstance(team, dict) and team.get("won"):
                return team.get("team_id", "")
    elif isinstance(teams, dict):
        for team_name, team_data in teams.items():
            if isinstance(team_data, dict) and team_data.get("has_won"):
                return team_name
    return ""


class RRTracker(commands.Cog):
    """Tracks Valorant RR gains and losses for registered members."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.api_key = os.getenv("HENRIK_API_KEY", "")
        self._recently_posted: set[str] = (
            set()
        )  # guard against duplicate posts — stores 'discord_id:match_id'
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
                if resp.status == 429:
                    print(f"[RR Tracker] Rate limited on MMR for {name}#{tag}")
                    return None
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
                if resp.status == 429:
                    print(f"[RR Tracker] Rate limited on matches for {name}#{tag}")
                    return []
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

    async def _get_mmr_history(self, name: str, tag: str) -> list:
        """Lightweight poll endpoint - returns recent MMR changes with match IDs."""
        session = await self._get_session()
        url = f"{API_BASE}/valorant/v1/mmr-history/{REGION}/{quote(name)}/{quote(tag)}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception:
            return []

    async def _get_match_details(self, name: str, tag: str) -> dict | None:
        """Fetch full match details for KDA, score, player card - only called when new game detected."""
        matches = await self._get_matches(name, tag, count=1)
        return matches[0] if matches else None

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

            # Phase 1: lightweight poll - check MMR history for all accounts
            # Only 1 API call per account, 2s apart
            new_games = []
            for account in accounts:
                try:
                    new_game = await self._detect_new_game(account)
                    if new_game:
                        new_games.append((account, new_game))
                except Exception as e:
                    print(
                        f"[RR Tracker] Error polling {account.get('val_name')}#{account.get('val_tag')}: {e}"
                    )
                await asyncio.sleep(2)

            # Phase 2: process new games - heavier calls, but only for accounts that have new games
            for account, match_id in new_games:
                try:
                    await self._post_new_game(account, match_id, channel, guild)
                except Exception as e:
                    print(
                        f"[RR Tracker] Error posting {account.get('val_name')}#{account.get('val_tag')}: {e}"
                    )
                await asyncio.sleep(3)

    async def _detect_new_game(self, account: dict) -> str | None:
        """Lightweight check - returns match_id if a new game is found, else None."""
        name = account["val_name"]
        tag = account["val_tag"]

        history = await self._get_mmr_history(name, tag)
        if not history:
            return None

        latest_entry = history[0]
        match_id = latest_entry.get("match_id")
        game_start = latest_entry.get("date_raw", 0)
        last_id = account.get("last_match_id")
        last_start = account.get("last_game_start", 0)

        if not match_id or match_id == last_id:
            return None

        post_key = f"{account['discord_id']}:{match_id}"
        if post_key in self._recently_posted:
            return None

        if game_start and last_start and game_start < last_start:
            return None

        self._recently_posted.add(post_key)
        if len(self._recently_posted) > 100:
            self._recently_posted.pop()

        # Update stored match ID
        await self.bot.val_accounts_col.update_one(
            {"_id": account["_id"]},
            {"$set": {"last_match_id": match_id, "last_game_start": game_start}},
        )

        if last_id is None:
            print(f"[RR Tracker] First match ID stored for {name}#{tag}: {match_id}")
            return None

        print(f"[RR Tracker] New game detected for {name}#{tag}: {match_id}")
        return match_id

    async def _post_new_game(
        self,
        account: dict,
        match_id: str,
        channel: discord.TextChannel,
        guild: discord.Guild,
    ):
        """Heavy lifting - fetch MMR and match details, build and send embed."""
        name = account["val_name"]
        tag = account["val_tag"]

        # Fetch live MMR for accurate rank, RR and last_change
        rr_change = 0
        map_name = "Unknown"

        print(f"[RR Tracker] Fetching MMR for {name}#{tag}...")
        mmr = await self._get_mmr(name, tag)
        if mmr:
            tier_name = mmr["current"]["tier"]["name"]
            rr = mmr["current"]["rr"]
            rr_change = mmr["current"].get("last_change", rr_change)
            print(f"[RR Tracker] MMR OK for {name}#{tag}: {tier_name} {rr}RR")
        else:
            tier_name = "Unrated"
            rr = 0
            print(f"[RR Tracker] MMR failed for {name}#{tag}")

        # Fetch full match only for KDA, score, player card, won/loss
        print(f"[RR Tracker] Fetching match details for {name}#{tag}...")
        await asyncio.sleep(1)
        latest = await self._get_match_details(name, tag)
        print(
            f"[RR Tracker] Match details {'OK' if latest else 'FAILED'} for {name}#{tag}"
        )
        kills = deaths = assists = 0
        hs_pct = 0
        agent = "Unknown"
        score_str = "-"
        won = rr_change >= 0
        player_card_id = None

        if latest:
            raw_map = latest.get("metadata", {}).get("map", "Unknown")
            map_name = (
                raw_map if isinstance(raw_map, str) else raw_map.get("name", "Unknown")
            )
            puuid = account.get("puuid", "")
            raw_players = latest.get("players", [])
            if isinstance(raw_players, list):
                all_players = raw_players
            elif isinstance(raw_players, dict):
                all_players = raw_players.get("all_players", [])
                if not all_players:
                    all_players = []
                    for val in raw_players.values():
                        if isinstance(val, list):
                            all_players.extend(val)
            else:
                all_players = []

            player = next(
                (
                    p
                    for p in all_players
                    if isinstance(p, dict) and p.get("puuid") == puuid
                ),
                None,
            )
            if player:
                kills = player["stats"]["kills"]
                deaths = player["stats"]["deaths"]
                assists = player["stats"]["assists"]
                headshots = player["stats"].get("headshots", 0)
                bodyshots = player["stats"].get("bodyshots", 0)
                legshots = player["stats"].get("legshots", 0)
                total_shots = headshots + bodyshots + legshots
                hs_pct = round(headshots / total_shots * 100) if total_shots > 0 else 0
                agent = player.get("agent", {}).get("name") or player.get(
                    "character", "Unknown"
                )
                won = (
                    player.get("team_id") or player.get("team", "")
                ).lower() == _winning_team(latest).lower()
                player_card_id = player.get("player_card")

                player_team = (player.get("team_id") or player.get("team", "")).lower()
                rounds_won = rounds_lost = 0
                teams = latest.get("teams", {})
                if isinstance(teams, list):
                    for team in teams:
                        if isinstance(team, dict):
                            if team.get("team_id", "").lower() == player_team:
                                rounds_won = team.get("rounds_won", 0)
                            else:
                                rounds_lost = team.get("rounds_won", 0)
                elif isinstance(teams, dict):
                    for tname, tdata in teams.items():
                        if not isinstance(tdata, dict):
                            continue
                        if tname.lower() == player_team:
                            rounds_won = tdata.get("rounds_won", 0)
                        else:
                            rounds_lost = tdata.get("rounds_won", 0)
                score_str = f"{rounds_won}-{rounds_lost}"

        result_str = "WIN" if won else "LOSS"
        embed_colour = COLOUR_MAIN if won else 0x8B4A4A

        session = await self._get_session()
        card_url = (
            f"https://media.valorant-api.com/playercards/{player_card_id}/smallart.png"
            if player_card_id
            else None
        )
        agent_icon_url = await _get_agent_icon(session, agent)

        tracker_url = f"https://tracker.gg/valorant/match/{match_id}"
        embed = discord.Embed(
            title=f"{name}#{tag}  -  {result_str}",
            url=tracker_url,
            color=embed_colour,
        )
        if card_url:
            embed.set_author(name=f"{name}#{tag}", icon_url=card_url)
        else:
            embed.set_author(name=f"{name}#{tag}")
        embed.add_field(name="Rank", value=f"**{tier_name}**\n{rr} RR", inline=True)
        embed.add_field(name="Change", value=f"**{_rr_arrow(rr_change)}**", inline=True)
        embed.add_field(
            name="KDA", value=f"**{kills}/{deaths}/{assists}**", inline=True
        )
        embed.add_field(name="Map", value=f"**{map_name}**", inline=True)
        embed.add_field(name="Score", value=f"**{score_str}**", inline=True)
        embed.add_field(name="HS%", value=f"**{hs_pct}%**", inline=True)
        if agent_icon_url:
            embed.set_thumbnail(url=agent_icon_url)
        embed.set_footer(text=f"Reverie  -  {guild.name}")

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
