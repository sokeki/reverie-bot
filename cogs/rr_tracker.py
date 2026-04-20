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

API_BASE = "https://api.henrikdev.xyz"
PLATFORM = "pc"

# Valorant region -> TFT platform code
VAL_TO_TFT_REGION = {
    "eu": "euw1",
    "na": "na1",
    "ap": "sg2",
    "kr": "kr",
    "br": "br1",
    "latam": "la1",
}

# Valorant region mapping - display name -> Henrik API region code
VAL_REGION_MAP = {
    "EUW": "eu",
    "EUNE": "eu",
    "NA": "na",
    "AP": "ap",
    "KR": "kr",
    "BR": "br",
    "LATAM": "latam",
}

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
        )  # guard against duplicate posts — stores 'puuid:match_id'
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

    async def _get_mmr_v2(self, name: str, tag: str, region: str = "eu") -> dict | None:
        """Fetch v2 MMR data which includes by_season with number_of_games per act."""
        session = await self._get_session()
        url = f"{API_BASE}/valorant/v2/mmr/{region}/{quote(name)}/{quote(tag)}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data")
        except Exception:
            return None

    async def _get_mmr(
        self, name: str, tag: str, region: str = "eu"
    ) -> dict | None | str:
        session = await self._get_session()
        url = (
            f"{API_BASE}/valorant/v3/mmr/{region}/{PLATFORM}/{quote(name)}/{quote(tag)}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    print(f"[Val Tracker] Rate limited on MMR for {name}#{tag}")
                    return "rate_limited"
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data")
        except Exception:
            return None

    async def _get_matches(
        self, name: str, tag: str, count: int = 1, region: str = "eu"
    ) -> list:
        session = await self._get_session()
        url = f"{API_BASE}/valorant/v3/matches/{region}/{quote(name)}/{quote(tag)}?mode=competitive&size={count}"
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    print(f"[Val Tracker] Rate limited on matches for {name}#{tag}")
                    return "rate_limited"
                if resp.status != 200:
                    text = await resp.text()
                    print(
                        f"[Val Tracker] Matches API {resp.status} for {name}#{tag}: {text[:200]}"
                    )
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception as e:
            print(f"[Val Tracker] Matches request failed for {name}#{tag}: {e}")
            return []

    async def _get_mmr_history(self, name: str, tag: str, region: str = "eu") -> list:
        """Lightweight poll endpoint - returns recent MMR changes with match IDs."""
        session = await self._get_session()
        url = f"{API_BASE}/valorant/v1/mmr-history/{region}/{quote(name)}/{quote(tag)}"
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    print(f"[Val Tracker] Rate limited on MMR history for {name}#{tag}")
                    return []
                if resp.status != 200:
                    print(f"[Val Tracker] MMR history {resp.status} for {name}#{tag}")
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception as e:
            print(f"[Val Tracker] MMR history error for {name}#{tag}: {e}")
            return []

    async def _get_match_details(
        self, name: str, tag: str, region: str = "eu"
    ) -> dict | None:
        """Fetch full match details for KDA, score, player card - only called when new game detected."""
        matches = await self._get_matches(name, tag, count=1, region=region)
        return matches[0] if matches else None

    async def _cache_full_match(self, match_id: str) -> None:
        """Fetch and cache a full match by ID if not already cached."""
        existing = await self.bot.val_match_cache_col.find_one({"match_id": match_id})
        if existing:
            return
        session = await self._get_session()
        url = f"{API_BASE}/valorant/v2/match/{match_id}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    full_match = data.get("data", data)
                    rounds = full_match.get("rounds", [])
                    await self.bot.val_match_cache_col.insert_one(
                        {
                            "match_id": match_id,
                            "data": full_match,
                            "cached_at": datetime.now(timezone.utc),
                        }
                    )
                    print(
                        f"[Val Tracker] Cached {match_id[:8]}... ({len(rounds)} rounds)"
                    )
        except Exception as e:
            print(f"[Val Tracker] Cache error for {match_id[:8]}...: {e}")

    async def _get_full_matches(self, matches: list) -> list:
        """Return full match data from cache, fetching from API only if not cached."""
        session = await self._get_session()
        full = []
        for match in matches:
            match_id = match.get("metadata", {}).get("matchid") or match.get(
                "metadata", {}
            ).get("match_id")
            if not match_id:
                continue
            # Check cache first — only use if it has full round data
            cached = await self.bot.val_match_cache_col.find_one({"match_id": match_id})
            if cached and cached.get("has_rounds"):
                full.append(cached["data"])
                continue
            # Not cached — fetch from API
            url = f"{API_BASE}/valorant/v2/match/{match_id}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        full_match = data.get("data", data)
                        rounds = full_match.get("rounds", [])
                        print(
                            f"[Val Tracker] Fetched & cached {match_id[:8]}... ({len(rounds)} rounds)"
                        )
                        # Extract puuid from match participants
                        raw_p = full_match.get("players", {})
                        all_p_c = (
                            raw_p
                            if isinstance(raw_p, list)
                            else raw_p.get("all_players", [])
                        )
                        match_puuids = [
                            p.get("puuid") for p in all_p_c if isinstance(p, dict)
                        ]
                        await self.bot.val_match_cache_col.update_one(
                            {"match_id": match_id},
                            {
                                "$set": {
                                    "data": full_match,
                                    "has_rounds": True,
                                    "cached_at": datetime.now(timezone.utc),
                                }
                            },
                            upsert=True,
                        )
                        full.append(full_match)
                    else:
                        print(
                            f"[Val Tracker] Match fetch {resp.status} for {match_id[:8]}..."
                        )
            except Exception as e:
                print(f"[Val Tracker] Match fetch error: {e}")
            await asyncio.sleep(1)
        return full

    async def _trim_match_cache(self, puuid: str) -> None:
        """Keep only the last 20 cached matches per player."""
        docs = (
            await self.bot.val_match_cache_col.find(
                {"$or": [{"puuid": puuid}, {"puuids": puuid}]}
            )
            .sort("cached_at", -1)
            .skip(20)
            .to_list(length=1000)
        )
        if docs:
            ids = [d["_id"] for d in docs]
            await self.bot.val_match_cache_col.delete_many({"_id": {"$in": ids}})

    # ── /registerriot ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="registerriot",
        description="Add a Riot account to server tracking (Valorant RR + TFT)",
    )
    @app_commands.describe(
        username="Riot ID including tag, e.g. Name#EUW",
        region="Server region",
    )
    @app_commands.choices(
        region=[app_commands.Choice(name=r, value=r) for r in VAL_REGION_MAP]
    )
    async def registerriot(
        self, interaction: discord.Interaction, username: str, region: str = "EUW"
    ):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Include the tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        val_region = VAL_REGION_MAP[region]
        await interaction.response.defer(ephemeral=True)

        mmr = await self._get_mmr(name, tag, val_region)
        if not mmr:
            await interaction.followup.send(
                f"⚠️ Couldn't find **{username}** on **{region}**. Check the spelling and try again.",
                ephemeral=True,
            )
            return

        tier = mmr["current"]["tier"]["name"]
        rr = mmr["current"]["rr"]
        puuid = mmr["account"]["puuid"]

        existing = await self.bot.riot_accounts_col.find_one(
            {"puuid": puuid, "guild_id": interaction.guild_id}
        )
        if existing:
            await interaction.followup.send(
                f"**{name}#{tag}** is already being tracked.", ephemeral=True
            )
            return

        # Fetch TFT LP baseline at registration
        tft_region = VAL_TO_TFT_REGION.get(val_region, "euw1")
        tft_lp = None
        try:
            import aiohttp as _aiohttp

            async with (await self._get_session()).get(
                f"https://{tft_region}.api.riotgames.com/tft/league/v1/by-puuid/{puuid}",
                headers={"X-Riot-Token": os.getenv("RIOT_API_KEY", "")},
            ) as resp:
                if resp.status == 200:
                    entries = await resp.json()
                    for e in entries:
                        if e.get("queueType") == "RANKED_TFT":
                            tier_order = {
                                "IRON": 0,
                                "BRONZE": 400,
                                "SILVER": 800,
                                "GOLD": 1200,
                                "PLATINUM": 1600,
                                "EMERALD": 2000,
                                "DIAMOND": 2400,
                                "MASTER": 2800,
                                "GRANDMASTER": 3200,
                                "CHALLENGER": 3600,
                            }
                            div_order = {"IV": 0, "III": 100, "II": 200, "I": 300}
                            tft_lp = (
                                tier_order.get(e["tier"].upper(), 0)
                                + div_order.get(e["rank"].upper(), 0)
                                + e["leaguePoints"]
                            )
        except Exception:
            pass

        # Fetch real Riot PUUID for TFT API (Henrik PUUID is different)
        riot_puuid = None
        try:
            routing = {
                "eu": "europe",
                "na": "americas",
                "ap": "asia",
                "kr": "asia",
                "br": "americas",
                "latam": "americas",
            }.get(val_region, "europe")
            async with (await self._get_session()).get(
                f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}",
                headers={"X-Riot-Token": os.getenv("RIOT_API_KEY", "")},
            ) as resp:
                if resp.status == 200:
                    riot_puuid = (await resp.json()).get("puuid")
        except Exception:
            pass

        doc = {
            "guild_id": interaction.guild_id,
            "val_name": name,
            "val_tag": tag,
            "val_region": val_region,
            "puuid": puuid,
            "riot_puuid": riot_puuid,
            "last_match_id": None,
            "last_game_start": 0,
            "tft": {
                "lp": tft_lp,
                "region": tft_region,
                "last_match_ids": [],
            },
        }
        await self.bot.riot_accounts_col.insert_one(doc)

        await interaction.followup.send(
            f"✅ Added **{name}#{tag}** ({region}) - currently **{tier}** at **{rr} RR**. "
            f"Games will be tracked automatically!",
            ephemeral=True,
        )

    # ── /unregisterriot ────────────────────────────────────────────────────────

    @app_commands.command(
        name="unregisterriot", description="Remove a Riot account from server tracking"
    )
    @app_commands.describe(username="Riot ID including tag, e.g. Name#EUW")
    async def unregisterriot(self, interaction: discord.Interaction, username: str):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Include the tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        result = await self.bot.riot_accounts_col.delete_one(
            {"guild_id": interaction.guild_id, "val_name": name, "val_tag": tag}
        )
        if result.deleted_count:
            await interaction.response.send_message(
                f"🌙 Removed **{name}#{tag}** from tracking.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"*couldn't find **{name}#{tag}** in the tracked list.*", ephemeral=True
            )

    # ── /setrrchannel ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="setvalchannel",
        description="[Admin] Set the channel for Valorant RR tracking updates",
    )
    @app_commands.describe(channel="Channel to post RR updates in")
    @app_commands.default_permissions(administrator=True)
    async def setvalchannel(
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

    # ── /rrleaderboard ───────────────────────────────────────────────────────

    @app_commands.command(
        name="valleaderboard",
        description="See the Valorant RR leaderboard for registered players",
    )
    async def valleaderboard(self, interaction: discord.Interaction):
        accounts = await self.bot.riot_accounts_col.find(
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
            mmr = await self._get_mmr(
                account["val_name"], account["val_tag"], account.get("val_region", "eu")
            )
            if not mmr or mmr == "rate_limited":
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
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
        await interaction.followup.send(embed=embed)

    # ── /rrtrackertest ───────────────────────────────────────────────────────

    @app_commands.command(
        name="valtrackertest",
        description="[Admin] Test the Valorant API for a specific account",
    )
    @app_commands.describe(username="Valorant username e.g. Name#TAG")
    @app_commands.default_permissions(administrator=True)
    async def valtrackertest(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if "#" not in username:
            await interaction.followup.send(
                "⚠️ Include the tag e.g. `Name#EUW`", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        from urllib.parse import quote

        session = await self._get_session()
        url = f"{API_BASE}/valorant/v3/matches/eu/{quote(name)}/{quote(tag)}?mode=competitive&size=1"
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
        name="valtrackerstatus",
        description="[Admin] Check if the Valorant tracker is running",
    )
    @app_commands.default_permissions(administrator=True)
    async def valtrackerstatus(self, interaction: discord.Interaction):
        accounts = await self.bot.riot_accounts_col.find(
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
                    f"- {a['val_name']}#{a['val_tag']} ({a.get('val_region', 'eu').upper()}) - last match ID: `{a.get('last_match_id') or 'null'}`"
                )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /valstats ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="valstats", description="Show Valorant stats for any player"
    )
    @app_commands.describe(
        username="Riot ID including tag, e.g. Name#EUW",
        region="Server region",
        detail="Detailed stats view",
    )
    @app_commands.choices(
        region=[app_commands.Choice(name=r, value=r) for r in VAL_REGION_MAP],
        detail=[
            app_commands.Choice(name="Clutch & First Bloods", value="clutch"),
            app_commands.Choice(name="Utility Usage", value="utility"),
            app_commands.Choice(name="Behaviour", value="behaviour"),
            app_commands.Choice(name="Agent Stats", value="agents"),
            app_commands.Choice(name="Map Winrates", value="maps"),
        ],
    )
    async def valstats(
        self,
        interaction: discord.Interaction,
        username: str,
        region: str = "EUW",
        detail: str = None,
    ):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Include the tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        val_region = VAL_REGION_MAP[region]
        await interaction.response.defer()

        # Always fetch MMR for rank/peak/season
        mmr = await self._get_mmr(name, tag, val_region)
        if mmr == "rate_limited":
            await interaction.followup.send(
                "⚠️ The API is currently rate limited - please try again in a minute.",
                ephemeral=True,
            )
            return
        if not mmr:
            await interaction.followup.send(
                f"⚠️ Couldn't find **{username}** on **{region}**."
            )
            return

        current = mmr.get("current", {})
        tier = current.get("tier", {}).get("name", "Unrated")
        rr = current.get("rr", 0)
        peak = mmr.get("peak", {})
        peak_tier = peak.get("tier", {}).get("name", "?") if peak else "?"
        peak_season = peak.get("season", {}).get("short", "") if peak else ""
        peak_str = f"{peak_tier} ({peak_season})" if peak_season else peak_tier

        # Use v2 MMR for season stats — by_season has number_of_games per act
        s_wins = s_losses = s_games = 0
        mmr_v2 = await self._get_mmr_v2(name, tag, val_region)
        if mmr_v2:
            by_season = mmr_v2.get("by_season", {})
            if by_season:
                # Get the most recent non-errored act
                current_act = next(
                    (
                        data
                        for act, data in sorted(by_season.items(), reverse=True)
                        if not data.get("error", False)
                        and data.get("number_of_games", 0) > 0
                    ),
                    None,
                )
                if current_act:
                    s_wins = current_act.get("wins", 0)
                    s_games = current_act.get("number_of_games", 0)
                    s_losses = s_games - s_wins
        else:
            # Fallback to v3 seasonal
            seasonal = mmr.get("seasonal", [])
            if seasonal:
                s = seasonal[0]
                s_wins = s.get("wins", 0)
                s_games = s.get("games", 0)
                s_losses = s_games - s_wins
        s_wr = round(s_wins / s_games * 100, 1) if s_games > 0 else 0

        # Fetch last 10 competitive matches and cache any new ones
        fresh = await self._get_matches(name, tag, count=10, region=val_region)
        if fresh == "rate_limited":
            await interaction.followup.send(
                "⚠️ The API is currently rate limited - please try again in a minute.",
                ephemeral=True,
            )
            return
        if not isinstance(fresh, list):
            fresh = []

        # Find puuid from fresh matches
        puuid = None
        for match in fresh:
            raw = match.get("players", [])
            all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
            for p in all_p:
                if isinstance(p, dict):
                    pname = p.get("name") or p.get("gameName", "")
                    ptag = p.get("tag") or p.get("tagLine", "")
                    if pname.lower() == name.lower() and ptag.lower() == tag.lower():
                        puuid = p.get("puuid")
                        break
            if puuid:
                break

        # Cache any new matches from fresh fetch and trim to 20 per player
        inserted = False
        for match in fresh:
            match_id = match.get("metadata", {}).get("matchid") or match.get(
                "metadata", {}
            ).get("match_id")
            if match_id and puuid:
                exists = await self.bot.val_match_cache_col.find_one(
                    {"match_id": match_id}
                )
                if not exists:
                    raw_p_f = match.get("players", [])
                    all_p_f = (
                        raw_p_f
                        if isinstance(raw_p_f, list)
                        else raw_p_f.get("all_players", [])
                    )
                    match_puuids = [
                        p.get("puuid")
                        for p in all_p_f
                        if isinstance(p, dict) and p.get("puuid")
                    ]
                    await self.bot.val_match_cache_col.insert_one(
                        {
                            "match_id": match_id,
                            "puuid": puuid,
                            "puuids": match_puuids,
                            "data": match,
                            "cached_at": datetime.now(timezone.utc),
                            "has_rounds": False,
                        }
                    )
                    inserted = True
        if inserted and puuid:
            await self._trim_match_cache(puuid)

        # Pull up to last 20 cached matches for this player for stats calculation
        # Uses puuid to find matches where the player appeared
        matches = []
        if puuid:
            cached_docs = (
                await self.bot.val_match_cache_col.find(
                    {"$or": [{"puuid": puuid}, {"puuids": puuid}]}
                )
                .sort("cached_at", -1)
                .limit(20)
                .to_list(length=20)
            )
            # Fallback: if the query returns nothing (v3 format stores players differently)
            # just use the fresh matches
            if cached_docs:
                matches = [doc["data"] for doc in cached_docs]
            else:
                matches = fresh

        if not matches:
            matches = fresh

        # Base stats across all matches
        total_hs = total_bs = total_ls = total_kills = total_deaths = total_assists = 0
        total_score = total_rounds = games_counted = 0
        for match in matches:
            rounds_played = match.get("metadata", {}).get("rounds_played", 0)
            raw = match.get("players", [])
            all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
            player = next(
                (p for p in all_p if isinstance(p, dict) and p.get("puuid") == puuid),
                None,
            )
            if not player:
                continue
            s = player.get("stats", {})
            total_kills += s.get("kills", 0)
            total_deaths += s.get("deaths", 0)
            total_assists += s.get("assists", 0)
            total_hs += s.get("headshots", 0)
            total_bs += s.get("bodyshots", 0)
            total_ls += s.get("legshots", 0)
            total_score += s.get("score", 0)
            total_rounds += rounds_played
            games_counted += 1

        avg_acs = round(total_score / total_rounds) if total_rounds > 0 else 0
        total_shots = total_hs + total_bs + total_ls
        hs_pct = round(total_hs / total_shots * 100) if total_shots > 0 else 0
        kda = round((total_kills + total_assists / 2) / max(total_deaths, 1), 2)
        colour = _tier_colour(tier)

        if not isinstance(matches, list) or (not matches and detail):
            await interaction.followup.send(
                "⚠️ The API is currently rate limited - please try again in a minute.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title=f"{name}#{tag}  -  Valorant Stats", color=colour)

        if not detail:
            embed.add_field(name="Rank", value=f"**{tier}**\n{rr} RR", inline=True)
            embed.add_field(name="Peak", value=f"**{peak_str}**", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(
                name="Season",
                value=f"**{s_wins}W {s_losses}L** ({s_wr}%)\n{s_games} games",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="ACS", value=f"**{avg_acs}**", inline=True)
            embed.add_field(name="KDA", value=f"**{kda}**", inline=True)
            embed.add_field(name="HS%", value=f"**{hs_pct}%**", inline=True)
            embed.set_footer(
                text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
            )

        elif detail == "clutch":
            full_matches = await self._get_full_matches(matches)
            clutch_opps = clutch_wins = first_bloods = 0
            for match in full_matches:
                rounds = match.get("rounds", [])
                raw = match.get("players", [])
                all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
                # Build puuid -> team map
                team_map = {
                    p["puuid"]: p.get("team", "").lower()
                    for p in all_p
                    if isinstance(p, dict)
                }
                player_team = team_map.get(puuid, "")
                if not player_team:
                    continue

                # Group top-level kills by round number
                all_kills = match.get("kills", [])
                kills_by_round: dict[int, list] = {}
                for k in all_kills:
                    kills_by_round.setdefault(k.get("round", 0), []).append(k)

                for rnd_idx, rnd in enumerate(rounds):
                    rnd_winner = rnd.get("winning_team", "").lower()
                    rnd_kills = sorted(
                        kills_by_round.get(rnd_idx, []),
                        key=lambda k: k.get("time_in_round_in_ms", 0),
                    )

                    # First blood
                    if rnd_kills and rnd_kills[0].get("killer_puuid") == puuid:
                        first_bloods += 1

                    # Clutch — who is still alive at end of round?
                    dead_puuids = {k.get("victim_puuid") for k in rnd_kills}
                    alive_teammates = [
                        p.get("puuid")
                        for p in all_p
                        if isinstance(p, dict)
                        and team_map.get(p.get("puuid", "")) == player_team
                        and p.get("puuid") != puuid
                        and p.get("puuid") not in dead_puuids
                    ]
                    player_alive = puuid not in dead_puuids

                    if player_alive and len(alive_teammates) == 0:
                        clutch_opps += 1
                        if rnd_winner == player_team:
                            clutch_wins += 1

            clutch_pct = (
                round(clutch_wins / clutch_opps * 100) if clutch_opps > 0 else 0
            )
            fb_pg = round(first_bloods / games_counted, 2) if games_counted > 0 else 0
            embed.add_field(name="Rank", value=f"**{tier}** {rr} RR", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(
                name="Clutch %",
                value=f"**{clutch_pct}%**\n{clutch_wins}/{clutch_opps}",
                inline=True,
            )
            embed.add_field(
                name="First Bloods/g",
                value=f"**{fb_pg}**\n{first_bloods} total",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.set_footer(
                text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
            )

        elif detail == "utility":
            full_matches = await self._get_full_matches(matches)
            total_c = total_q = total_e = total_x = 0
            for match in full_matches:
                raw = match.get("players", [])
                all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
                player = next(
                    (
                        p
                        for p in all_p
                        if isinstance(p, dict) and p.get("puuid") == puuid
                    ),
                    None,
                )
                if not player:
                    continue
                casts = player.get("ability_casts") or {}
                total_c += casts.get("c_cast", 0)
                total_q += casts.get("q_cast", 0)
                total_e += casts.get("e_cast", 0)
                total_x += casts.get("x_cast", 0)
            g = max(games_counted, 1)
            embed.add_field(name="Rank", value=f"**{tier}** {rr} RR", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(
                name="C Cast",
                value=f"**{round(total_c/g,1)}/g**\n{total_c} total",
                inline=True,
            )
            embed.add_field(
                name="Q Cast",
                value=f"**{round(total_q/g,1)}/g**\n{total_q} total",
                inline=True,
            )
            embed.add_field(
                name="E Cast",
                value=f"**{round(total_e/g,1)}/g**\n{total_e} total",
                inline=True,
            )
            embed.add_field(
                name="X Cast",
                value=f"**{round(total_x/g,1)}/g**\n{total_x} total",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.set_footer(
                text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
            )

        elif detail == "behaviour":
            full_matches = await self._get_full_matches(matches)
            total_afk = total_spawn = ff_out = ff_in = total_rounds_beh = 0
            for match_idx, match in enumerate(full_matches):
                raw = match.get("players", [])
                all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
                player = next(
                    (
                        p
                        for p in all_p
                        if isinstance(p, dict) and p.get("puuid") == puuid
                    ),
                    None,
                )
                if not player:
                    print(
                        f"[Val Tracker] Behaviour: player not found in match {match_idx}"
                    )
                    continue
                rounds_played = match.get("metadata", {}).get("rounds_played", 0)
                total_rounds_beh += rounds_played
                # Count AFK and spawn rounds from per-round player_stats
                for rnd in match.get("rounds", []):
                    for ps in rnd.get("player_stats", []):
                        if ps.get("player_puuid") == puuid:
                            if ps.get("was_afk"):
                                total_afk += 1
                            if ps.get("stayed_in_spawn"):
                                total_spawn += 1
                            break
                # FF from behavior field
                beh = player.get("behavior") or {}
                ff = beh.get("friendly_fire") or {}
                ff_out += ff.get("outgoing", 0)
                ff_in += ff.get("incoming", 0)
            g = max(games_counted, 1)
            embed.add_field(name="Rank", value=f"**{tier}** {rr} RR", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(
                name="AFK Rounds/g",
                value=f"**{round(total_afk/g,1)}**\n{int(total_afk)}/{total_rounds_beh} rounds",
                inline=True,
            )
            embed.add_field(
                name="Spawn Rounds/g",
                value=f"**{round(total_spawn/g,1)}**\n{int(total_spawn)}/{total_rounds_beh} rounds",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(
                name="FF Outgoing/g",
                value=f"**{round(ff_out/g,1)}**\n{int(ff_out)} total",
                inline=True,
            )
            embed.add_field(
                name="FF Incoming/g",
                value=f"**{round(ff_in/g,1)}**\n{int(ff_in)} total",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.set_footer(
                text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
            )

        elif detail == "agents":
            agent_stats: dict[str, dict] = {}
            for match in matches:
                raw = match.get("players", [])
                all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
                player = next(
                    (
                        p
                        for p in all_p
                        if isinstance(p, dict) and p.get("puuid") == puuid
                    ),
                    None,
                )
                if not player:
                    continue
                agent = player.get("agent", {}).get("name") or player.get(
                    "character", "Unknown"
                )
                s = player.get("stats", {})
                rounds = match.get("metadata", {}).get("rounds_played", 1)
                won = (
                    player.get("team_id") or player.get("team", "")
                ).lower() == _winning_team(match).lower()
                acs = round(s.get("score", 0) / max(rounds, 1))
                if agent not in agent_stats:
                    agent_stats[agent] = {
                        "games": 0,
                        "wins": 0,
                        "kills": 0,
                        "deaths": 0,
                        "acs": 0,
                    }
                ag = agent_stats[agent]
                ag["games"] += 1
                ag["wins"] += 1 if won else 0
                ag["kills"] += s.get("kills", 0)
                ag["deaths"] += s.get("deaths", 0)
                ag["acs"] += acs
            sorted_agents = sorted(
                agent_stats.items(), key=lambda x: x[1]["games"], reverse=True
            )
            lines = []
            for agent, st in sorted_agents[:8]:
                g_ = st["games"]
                wr_ = round(st["wins"] / g_ * 100)
                kd_ = round(st["kills"] / max(st["deaths"], 1), 2)
                acs_ = round(st["acs"] / g_)
                lines.append(f"**{agent}** ({g_}g)  {wr_}% WR  {kd_} KD  {acs_} ACS")
            embed.description = "\n".join(lines) if lines else "*no data*"
            embed.set_footer(
                text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
            )

        elif detail == "maps":
            map_stats: dict[str, dict] = {}
            for match in matches:
                raw_map = match.get("metadata", {}).get("map", "Unknown")
                map_name = (
                    raw_map
                    if isinstance(raw_map, str)
                    else raw_map.get("name", "Unknown")
                )
                raw = match.get("players", [])
                all_p = raw if isinstance(raw, list) else raw.get("all_players", [])
                player = next(
                    (
                        p
                        for p in all_p
                        if isinstance(p, dict) and p.get("puuid") == puuid
                    ),
                    None,
                )
                if not player:
                    continue
                won = (
                    player.get("team_id") or player.get("team", "")
                ).lower() == _winning_team(match).lower()
                if map_name not in map_stats:
                    map_stats[map_name] = {"games": 0, "wins": 0}
                map_stats[map_name]["games"] += 1
                map_stats[map_name]["wins"] += 1 if won else 0
            sorted_maps = sorted(
                map_stats.items(), key=lambda x: x[1]["games"], reverse=True
            )
            lines = []
            for map_name, st in sorted_maps:
                g_ = st["games"]
                wr_ = round(st["wins"] / g_ * 100)
                w_ = st["wins"]
                l_ = g_ - w_
                lines.append(f"**{map_name}** ({g_}g)  {wr_}% WR  {w_}W {l_}L")
            embed.description = "\n".join(lines) if lines else "*no data*"
            embed.set_footer(
                text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
            )

        await interaction.followup.send(embed=embed)

    # ── /footshot ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="footshot",
        description="Check a player's shot accuracy across their last 10 competitive games",
    )
    @app_commands.describe(username="Valorant username and tag, e.g. Name#EUW")
    async def footshot(self, interaction: discord.Interaction, username: str):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Please include the tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        await interaction.response.defer()

        session = await self._get_session()
        url = f"{API_BASE}/valorant/v3/matches/eu/{quote(name)}/{quote(tag)}?mode=competitive&size=10"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await interaction.followup.send(
                        f"⚠️ Couldn't find **{username}** on EU servers.", ephemeral=True
                    )
                    return
                data = await resp.json()
                matches = data.get("data", [])
        except Exception as e:
            await interaction.followup.send(f"⚠️ API error: {e}", ephemeral=True)
            return

        if not matches:
            await interaction.followup.send(
                f"*no recent competitive games found for **{username}**.*",
                ephemeral=True,
            )
            return

        # Find the player's puuid from the first match
        puuid = None
        for match in matches:
            raw_players = match.get("players", [])
            all_players = (
                raw_players
                if isinstance(raw_players, list)
                else raw_players.get("all_players", [])
            )
            for p in all_players:
                if isinstance(p, dict):
                    pname = p.get("name") or p.get("gameName", "")
                    ptag = p.get("tag") or p.get("tagLine", "")
                    if pname.lower() == name.lower() and ptag.lower() == tag.lower():
                        puuid = p.get("puuid")
                        break
            if puuid:
                break

        if not puuid:
            await interaction.followup.send(
                f"⚠️ Couldn't identify **{username}** in their match history.",
                ephemeral=True,
            )
            return

        # Accumulate shot stats across all matches
        total_hs = total_bs = total_ls = 0
        games_counted = 0

        for match in matches:
            raw_players = match.get("players", [])
            all_players = (
                raw_players
                if isinstance(raw_players, list)
                else raw_players.get("all_players", [])
            )
            player = next(
                (
                    p
                    for p in all_players
                    if isinstance(p, dict) and p.get("puuid") == puuid
                ),
                None,
            )
            if not player:
                continue
            stats = player.get("stats", {})
            hs = stats.get("headshots", 0)
            bs = stats.get("bodyshots", 0)
            ls = stats.get("legshots", 0)
            total = hs + bs + ls
            if total > 0:
                total_hs += hs
                total_bs += bs
                total_ls += ls
                games_counted += 1

        if games_counted == 0:
            await interaction.followup.send(
                f"*no shot data found for **{username}**.*", ephemeral=True
            )
            return

        grand_total = total_hs + total_bs + total_ls
        hs_pct = round(total_hs / grand_total * 100)
        bs_pct = round(total_bs / grand_total * 100)
        ls_pct = round(total_ls / grand_total * 100)

        embed = discord.Embed(
            title=f"{name}#{tag}  -  Shot Accuracy",
            color=COLOUR_MAIN,
        )
        embed.add_field(name="Headshot %", value=f"**{hs_pct}%**", inline=True)
        embed.add_field(name="Body %", value=f"**{bs_pct}%**", inline=True)
        embed.add_field(name="Leg %", value=f"**{ls_pct}%**", inline=True)
        embed.set_footer(
            text=f"Last {games_counted} competitive games  •  Reverie  •  {interaction.guild.name}"
        )
        await interaction.followup.send(embed=embed)

        # ── /scoreboard ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="scoreboard",
        description="Show the scoreboard for a match. Provide a match ID or a username to use their latest game.",
    )
    @app_commands.describe(
        match_id="Match ID from the RR update footer",
        username="Valorant username and tag to use their latest game, e.g. Name#EUW",
    )
    async def scoreboard(
        self,
        interaction: discord.Interaction,
        match_id: str = None,
        username: str = None,
    ):
        if not match_id and not username:
            await interaction.response.send_message(
                "⚠️ Provide either a match ID or a username.", ephemeral=True
            )
            return

        await interaction.response.defer()
        session = await self._get_session()

        if match_id:
            # Fetch match directly by ID
            url = f"{API_BASE}/valorant/v2/match/{match_id}"
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await interaction.followup.send(
                            f"⚠️ Couldn't find match `{match_id}`.", ephemeral=True
                        )
                        return
                    data = await resp.json()
                    match = data.get("data", data)
            except Exception as e:
                await interaction.followup.send(f"⚠️ API error: {e}", ephemeral=True)
                return
        else:
            if "#" not in username:
                await interaction.followup.send(
                    "⚠️ Include the tag, e.g. `Name#EUW`.", ephemeral=True
                )
                return
            name, tag = username.split("#", 1)
            matches = await self._get_matches(name, tag, count=1)
            if not matches:
                await interaction.followup.send(
                    f"⚠️ No recent matches found for **{username}**.", ephemeral=True
                )
                return
            match = matches[0]

        embed = self._build_scoreboard_embed(match, interaction.guild)
        await interaction.followup.send(embed=embed)

    def _build_scoreboard_embed(
        self, match: dict, guild: discord.Guild
    ) -> discord.Embed:
        """Build a scoreboard embed from a match data dict."""
        metadata = match.get("metadata", {})
        raw_map = metadata.get("map", "Unknown")
        map_name = (
            raw_map if isinstance(raw_map, str) else raw_map.get("name", "Unknown")
        )
        rounds = metadata.get("rounds_played", "?")
        match_id = metadata.get("match_id") or metadata.get("matchid", "")

        raw_players = match.get("players", [])
        if isinstance(raw_players, list):
            all_players = raw_players
        elif isinstance(raw_players, dict):
            all_players = raw_players.get("all_players", [])
        else:
            all_players = []

        teams: dict[str, list] = {}
        for p in all_players:
            if not isinstance(p, dict):
                continue
            team = (p.get("team_id") or p.get("team", "?")).upper()
            teams.setdefault(team, []).append(p)

        for team in teams.values():
            team.sort(key=lambda p: p.get("stats", {}).get("score", 0), reverse=True)

        team_scores: dict[str, int] = {}
        raw_teams = match.get("teams", {})
        if isinstance(raw_teams, list):
            for t in raw_teams:
                if isinstance(t, dict):
                    tid = (t.get("team_id") or "?").upper()
                    team_scores[tid] = t.get("rounds_won", 0)
        elif isinstance(raw_teams, dict):
            for tid, tdata in raw_teams.items():
                if isinstance(tdata, dict):
                    team_scores[tid.upper()] = tdata.get("rounds_won", 0)

        def _short_rank(rank: str) -> str:
            short = {
                "Iron": "Iron",
                "Bronze": "Brnz",
                "Silver": "Silv",
                "Gold": "Gold",
                "Platinum": "Plat",
                "Diamond": "Dia",
                "Ascendant": "Asc",
                "Immortal": "Imm",
                "Radiant": "Rad",
            }
            for full, abbr in short.items():
                if rank.startswith(full):
                    return rank.replace(full, abbr)
            return rank[:8]

        def build_table(players: list) -> str:
            header = f"{'Player':<12} {'Agent':<9} {'Rank':<7} {'K':>3} {'D':>3} {'A':>3} {'ACS':>4} {'HS%':>4}"
            divider = "-" * len(header)
            rows = [header, divider]
            for p in players:
                stats = p.get("stats", {})
                pname = (p.get("name") or p.get("gameName", "?"))[:11]
                agent = (p.get("agent", {}).get("name") or p.get("character", "?"))[:9]
                rank = _short_rank(p.get("currenttier_patched") or "?")
                k = stats.get("kills", 0)
                d = stats.get("deaths", 0)
                a = stats.get("assists", 0)
                score = stats.get("score", 0)
                rounds_ = max(rounds if isinstance(rounds, int) else 1, 1)
                acs = round(score / rounds_)
                hs = stats.get("headshots", 0)
                bs = stats.get("bodyshots", 0)
                ls = stats.get("legshots", 0)
                total = hs + bs + ls
                hs_pct = f"{round(hs/total*100)}%" if total > 0 else "0%"
                rows.append(
                    f"{pname:<12} {agent:<9} {rank:<7} {k:>3} {d:>3} {a:>3} {acs:>4} {hs_pct:>4}"
                )
            return "```\n" + "\n".join(rows) + "\n```"

        embed = discord.Embed(
            title=map_name,
            url=f"https://tracker.gg/valorant/match/{match_id}",
            color=COLOUR_MAIN,
        )
        for team_id, players in sorted(teams.items()):
            score = team_scores.get(team_id, "?")
            won = score == max(team_scores.values()) if team_scores else False
            label = f"Team {team_id}  -  {score} rounds"
            embed.add_field(name=label, value=build_table(players), inline=False)
        embed.set_footer(text=f"Reverie  •  {guild.name}")
        return embed

    # ── r!scoreboard prefix command ───────────────────────────────────────────

    @commands.command(name="sb")
    async def scoreboard_prefix(self, ctx: commands.Context):
        """Reply to an RR update embed to show its scoreboard."""
        match_id = None

        # Try to extract match ID from a replied-to message
        if ctx.message.reference:
            try:
                ref_msg = await ctx.channel.fetch_message(
                    ctx.message.reference.message_id
                )
                # Look for match ID in embed footer
                for embed in ref_msg.embeds:
                    if embed.footer and embed.footer.text:
                        # Footer format: "match_id  -  Reverie  -  guild"
                        parts = embed.footer.text.split("  •  ")
                        if parts and len(parts[0]) > 30:  # UUID length check
                            match_id = parts[0].strip()
                            break
            except Exception:
                pass

        if not match_id:
            await ctx.reply(
                "⚠️ Reply to an RR update embed to pull up the scoreboard, or use `/scoreboard match_id:...`."
            )
            return

        session = await self._get_session()
        url = f"{API_BASE}/valorant/v2/match/{match_id}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await ctx.reply(f"⚠️ Couldn't find match `{match_id}`.")
                    return
                data = await resp.json()
                match = data.get("data", data)
        except Exception as e:
            await ctx.reply(f"⚠️ API error: {e}")
            return

        embed = self._build_scoreboard_embed(match, ctx.guild)
        await ctx.reply(embed=embed)

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
            accounts = await self.bot.riot_accounts_col.find(
                {"guild_id": guild.id}
            ).to_list(length=100)

            # Phase 1: lightweight poll - check MMR history for all accounts
            # Collect all new match IDs found this cycle
            new_games: list[tuple[dict, str]] = []
            for account in accounts:
                try:
                    new_game = await self._detect_new_game(account)
                    if new_game:
                        new_games.append(
                            (
                                account,
                                new_game[0],
                                new_game[1],
                                new_game[2],
                                new_game[3],
                            )
                        )
                except Exception as e:
                    print(
                        f"[Val Tracker] Error polling {account.get('val_name')}#{account.get('val_tag')}: {e}"
                    )
                await asyncio.sleep(1)

            if not new_games:
                continue

            # Phase 2: group by match_id and fetch each unique match once
            # Then find all registered accounts in that match and post for each
            matches_fetched: dict[str, dict] = {}
            by_match: dict[str, list[dict]] = {}
            for account, match_id, *_ in new_games:
                by_match.setdefault(match_id, []).append(account)

            for match_id, detected_accounts in by_match.items():
                # Fetch match once for this match_id
                first = detected_accounts[0]
                val_region = first.get("val_region", "eu")
                if match_id not in matches_fetched:
                    match_data = await self._get_match_details(
                        first["val_name"], first["val_tag"], val_region
                    )
                    matches_fetched[match_id] = match_data
                    await asyncio.sleep(1)
                    await self._cache_full_match(match_id)
                match_data = matches_fetched[match_id]

                # Get all puuids in this match
                all_puuids_in_match = set()
                match_game_start = 0
                if match_data:
                    raw_players = match_data.get("players", [])
                    all_players = (
                        raw_players
                        if isinstance(raw_players, list)
                        else raw_players.get("all_players", [])
                    )
                    all_puuids_in_match = {
                        p.get("puuid") for p in all_players if isinstance(p, dict)
                    }
                    match_game_start = match_data.get("metadata", {}).get(
                        "game_start", 0
                    )
                print(
                    f"[Val Tracker] Match scan: {len(all_puuids_in_match)} players, {len(accounts)} registered accounts"
                )

                # Puuids directly detected in phase 1 for this match
                detected_puuids = {
                    a.get("puuid", "") for a, mid, *_ in new_games if mid == match_id
                }

                # Single pass: find registered accounts in match, update DB, collect to post
                accounts_to_post = []
                for account in accounts:
                    puuid = account.get("puuid", "")
                    post_key = f"{puuid}:{match_id}"
                    last_id = account.get("last_match_id")

                    if puuid not in all_puuids_in_match:
                        continue
                    if post_key in self._recently_posted:
                        continue
                    # Skip if already has this match — unless it was directly detected
                    # (directly detected accounts have last_match_id already updated in memory)
                    if last_id == match_id and puuid not in detected_puuids:
                        continue

                    self._recently_posted.add(post_key)
                    if len(self._recently_posted) > 200:
                        self._recently_posted.pop()

                    await self.bot.riot_accounts_col.update_one(
                        {"_id": account["_id"]},
                        {
                            "$set": {
                                "last_match_id": match_id,
                                "last_game_start": match_game_start,
                            }
                        },
                    )
                    account["last_match_id"] = match_id
                    account["last_game_start"] = match_game_start

                    if last_id is None and puuid not in detected_puuids:
                        continue  # first time baseline, not directly detected

                    if puuid not in detected_puuids:
                        print(
                            f"[Val Tracker] Found in match: {account.get('val_name')}#{account.get('val_tag')}: {match_id}"
                        )
                    accounts_to_post.append(account)

                # Build lookup of rr data from new_games detections
                rr_data = {
                    a.get("puuid", ""): (rc, r, t)
                    for a, mid, rc, r, t in new_games
                    if mid == match_id
                }
                for account in accounts_to_post:
                    try:
                        puuid_ = account.get("puuid", "")
                        rc, r, t = rr_data.get(puuid_, (0, 0, ""))
                        await self._post_new_game(
                            account,
                            match_id,
                            channel,
                            guild,
                            match_data,
                            rr_change=rc,
                            rr=r,
                            tier_from_history=t,
                        )
                    except Exception as e:
                        import traceback

                        print(
                            f"[Val Tracker] Error posting {account.get('val_name')}#{account.get('val_tag')}: {e}"
                        )
                        traceback.print_exc()
                    await asyncio.sleep(2)

    async def _detect_new_game(self, account: dict) -> tuple | None:
        """Lightweight check - returns (match_id, rr_change, rr, tier) if new game found, else None."""
        name = account["val_name"]
        tag = account["val_tag"]

        region = account.get("val_region", "eu")
        history = await self._get_mmr_history(name, tag, region)
        if not history:
            return None

        latest_entry = history[0]
        match_id = latest_entry.get("match_id")
        game_start = latest_entry.get("date_raw", 0)
        last_id = account.get("last_match_id")
        last_start = account.get("last_game_start", 0)

        if not match_id or match_id == last_id:
            return None

        if game_start and last_start and game_start < last_start:
            return None

        # Update stored match ID and game_start — also update in-memory dict so
        # the match scan loop sees the updated last_match_id and won't double-post
        await self.bot.riot_accounts_col.update_one(
            {"_id": account["_id"]},
            {"$set": {"last_match_id": match_id, "last_game_start": game_start}},
        )
        account["last_match_id"] = match_id
        account["last_game_start"] = game_start

        if last_id is None:
            print(f"[Val Tracker] First match ID stored for {name}#{tag}: {match_id}")
            return None

        # Extract RR data from history entry to avoid a separate MMR API call
        rr_change = latest_entry.get("mmr_change_to_last_game", 0)
        rr = latest_entry.get("ranking_in_tier", 0)
        tier = latest_entry.get("currenttier_patched", "")

        print(
            f"[Val Tracker] New game detected for {name}#{tag}: {match_id} ({rr_change:+d}RR)"
        )
        return (match_id, rr_change, rr, tier)

    async def _post_new_game(
        self,
        account: dict,
        match_id: str,
        channel: discord.TextChannel,
        guild: discord.Guild,
        latest: dict = None,
        rr_change: int = 0,
        rr: int = 0,
        tier_from_history: str = "",
    ):
        """Heavy lifting - build and send embed using RR data from history entry."""
        name = account["val_name"]
        tag = account["val_tag"]
        map_name = "Unknown"

        val_region = account.get("val_region", "eu")
        puuid_acc = account.get("puuid", "")

        # Try to read rr_change from match data player object (most reliable)
        if rr_change == 0 and latest:
            raw_p = latest.get("players", [])
            all_p_m = raw_p if isinstance(raw_p, list) else raw_p.get("all_players", [])
            player_entry = next(
                (
                    p
                    for p in all_p_m
                    if isinstance(p, dict) and p.get("puuid") == puuid_acc
                ),
                None,
            )
            if player_entry:
                rr_change = player_entry.get("mmr_change_to_last_game", 0) or 0
                print(
                    f"[Val Tracker] RR change from match data for {name}#{tag}: {rr_change:+d}"
                )

        # Fallback: retry history until match appears (Henrik cache lag)
        if rr_change == 0:
            for attempt in range(12):  # up to 3 minutes (12 x 15s)
                await asyncio.sleep(15)
                print(
                    f"[Val Tracker] Fetching MMR history for {name}#{tag} (attempt {attempt + 1}/12)..."
                )
                history = await self._get_mmr_history(name, tag, val_region)
                if history:
                    entry = next(
                        (e for e in history if e.get("match_id") == match_id), None
                    )
                    if entry:
                        rr_change = entry.get("mmr_change_to_last_game", 0)
                        print(
                            f"[Val Tracker] MMR history OK for {name}#{tag}: rr_change={rr_change:+d}"
                        )
                        break
                    else:
                        print(
                            f"[Val Tracker] MMR history: not yet updated for {name}#{tag} (history[0]={history[0].get('match_id', '?')[:8]}...)"
                        )
            else:
                print(
                    f"[Val Tracker] MMR history: gave up after 3 minutes for {name}#{tag}"
                )

        # Live MMR for current tier/rr display
        print(f"[Val Tracker] Fetching MMR for {name}#{tag}...")
        mmr = await self._get_mmr(name, tag, val_region)
        if mmr and mmr != "rate_limited":
            tier_name = mmr["current"]["tier"]["name"]
            rr = mmr["current"]["rr"]
            print(f"[Val Tracker] MMR OK for {name}#{tag}: {tier_name} {rr}RR")
        else:
            tier_name = tier_from_history or "Unrated"
            print(f"[Val Tracker] MMR failed for {name}#{tag}, using history fallback")

        # Use pre-fetched match data if available
        if latest is None:
            print(f"[Val Tracker] Fetching match details for {name}#{tag}...")
            await asyncio.sleep(1)
            latest = await self._get_match_details(
                name, tag, account.get("val_region", "eu")
            )
            print(
                f"[Val Tracker] Match details {'OK' if latest else 'FAILED'} for {name}#{tag}"
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

        is_placement = (
            tier_name in ("Unrated", "Unranked", "") or rr == 0 and rr_change == 0
        )
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
        rank_display = "**Placement**" if is_placement else f"**{tier_name}**\n{rr} RR"
        change_display = "*-*" if is_placement else f"**{_rr_arrow(rr_change)}**"
        embed.add_field(name="Rank", value=rank_display, inline=True)
        embed.add_field(name="Change", value=change_display, inline=True)
        embed.add_field(
            name="KDA", value=f"**{kills}/{deaths}/{assists}**", inline=True
        )
        embed.add_field(name="Map", value=f"**{map_name}**", inline=True)
        embed.add_field(name="Score", value=f"**{score_str}**", inline=True)
        embed.add_field(name="HS%", value=f"**{hs_pct}%**", inline=True)
        if agent_icon_url:
            embed.set_thumbnail(url=agent_icon_url)
        embed.set_footer(text=f"{match_id}  •  Reverie  •  {guild.name}")

        await channel.send(embed=embed)

        # Cache full match data for /valstats detail views
        if latest and match_id:
            try:
                exists = await self.bot.val_match_cache_col.find_one(
                    {"match_id": match_id}
                )
                if not exists:
                    # Store all player puuids so any player can find this match
                    raw_p = latest.get("players", [])
                    all_p_c = (
                        raw_p
                        if isinstance(raw_p, list)
                        else raw_p.get("all_players", [])
                    )
                    all_puuids = [
                        p.get("puuid")
                        for p in all_p_c
                        if isinstance(p, dict) and p.get("puuid")
                    ]
                    await self.bot.val_match_cache_col.insert_one(
                        {
                            "match_id": match_id,
                            "puuid": account.get("puuid", ""),
                            "puuids": all_puuids,
                            "data": latest,
                            "cached_at": datetime.now(timezone.utc),
                            "has_rounds": True,
                        }
                    )
                    await self._trim_match_cache(account.get("puuid", ""))
            except Exception:
                pass

        # Store for daily summary
        await self.bot.val_games_col.insert_one(
            {
                "guild_id": guild.id,
                "puuid": account["puuid"],
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
        if now.hour != 0 or now.minute > 4:
            return
        today = now.strftime("%Y-%m-%d")
        print(
            f"[Daily Summary] Midnight window hit at {now.strftime('%H:%M')} UTC — date: {today}"
        )
        if getattr(self, "_last_summary_date", None) == today:
            print(f"[Daily Summary] Already posted for {today}, skipping")
            return
        self._last_summary_date = today
        for guild in self.bot.guilds:
            try:
                print(f"[Daily Summary] Posting for guild: {guild.name}")
                await self._post_daily_summary(guild)
            except Exception as e:
                import traceback

                print(f"[Daily Summary] Error for guild {guild.name}: {e}")
                traceback.print_exc()

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
        by_player: dict[str, list] = {}
        for g in games:
            key = g.get("puuid") or g.get("discord_id") or g.get("val_name", "?")
            by_player.setdefault(key, []).append(g)

        lines = []
        for player_key, player_games in sorted(
            by_player.items(),
            key=lambda kv: sum(g["rr_change"] for g in kv[1]),
            reverse=True,
        ):
            total_rr = sum(g["rr_change"] for g in player_games)
            wins = sum(1 for g in player_games if g["won"])
            losses = len(player_games) - wins
            win_rate = round(wins / len(player_games) * 100)
            first_game = player_games[0]
            last_game = player_games[-1]
            start_tier = first_game.get("tier", "")
            end_tier = last_game.get("tier", "")
            start_rr = max(0, first_game["rr_after"] - first_game["rr_change"])
            end_rr = last_game["rr_after"]
            name = first_game["val_name"]
            tag = first_game["val_tag"]
            rr_str = f"+{total_rr}" if total_rr >= 0 else str(total_rr)
            rank_str = (
                f"{start_tier} {start_rr}rr → {end_tier} {end_rr}rr"
                if start_tier != end_tier
                else f"{end_tier} {start_rr}rr → {end_rr}rr"
            )

            lines.append(
                f"**{name}#{tag}** : {rr_str}\n"
                f"{wins}W {losses}L ({win_rate}%) | {rank_str}"
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
