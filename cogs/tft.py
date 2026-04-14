"""
TFT Tracker cog - polls the Riot API every minute to detect LP changes
for registered TFT accounts and posts updates to a channel.

Required env var:
  RIOT_API_KEY  - your Riot Games API key

Set channel with /settftchannel
"""

import os
import asyncio
import aiohttp
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")

TIER_ORDER = {
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
DIVISION_ORDER = {"IV": 0, "III": 100, "II": 200, "I": 300}

COMPANIONS_URL = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/companions.json"


def _lp_total(tier: str, division: str, lp: int) -> int:
    """Convert tier/division/lp to a single comparable number."""
    return (
        TIER_ORDER.get(tier.upper(), 0) + DIVISION_ORDER.get(division.upper(), 0) + lp
    )


def _format_rank(tier: str, division: str, lp: int) -> str:
    """Format rank as a readable string."""
    t = tier.capitalize()
    if tier.upper() in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{t} {lp}LP"
    return f"{t} {division} {lp}LP"


def _lp_arrow(diff: int) -> str:
    if diff > 0:
        return f"+{diff}LP"
    return f"{diff}LP"


class RiotAPI:
    """Async Riot API client."""

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"X-Riot-Token": RIOT_API_KEY})
        return self.session

    async def _get(self, url: str, params: dict = None) -> dict | list | None:
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception:
            return None

    async def get_account(self, routing: str, name: str, tag: str) -> dict | None:
        url = f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
        return await self._get(url)

    async def get_league_entries(self, region: str, puuid: str) -> list:
        url = f"https://{region}.api.riotgames.com/tft/league/v1/by-puuid/{puuid}"
        result = await self._get(url)
        return result if isinstance(result, list) else []

    async def get_match_ids(self, routing: str, puuid: str, count: int = 5) -> list:
        url = f"https://{routing}.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids"
        result = await self._get(url, params={"count": count})
        return result if isinstance(result, list) else []

    async def get_match(self, routing: str, match_id: str) -> dict | None:
        url = f"https://{routing}.api.riotgames.com/tft/match/v1/matches/{match_id}"
        return await self._get(url)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


def _region_to_routing(region: str) -> str:
    mapping = {
        "euw1": "europe",
        "eun1": "europe",
        "na1": "americas",
        "la1": "americas",
        "br1": "americas",
        "sg2": "asia",
        "kr": "asia",
    }
    return mapping.get(region, "europe")


def _val_to_tft_region(val_region: str) -> str:
    """Convert Valorant Henrik region code to TFT Riot platform code."""
    mapping = {
        "eu": "euw1",
        "na": "na1",
        "ap": "sg2",
        "kr": "kr",
        "br": "br1",
        "latam": "la1",
    }
    return mapping.get(val_region, "euw1")


class TFTTracker(commands.Cog):
    """TFT LP tracker — monitors registered accounts and posts rank changes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.riot = RiotAPI()
        self._companions: dict[int, str] = {}  # item_ID -> icon URL
        self.poll_task.start()

    def cog_unload(self):
        self.poll_task.cancel()
        self.bot.loop.create_task(self.riot.close())

    # ── /settftchannel ────────────────────────────────────────────────────────

    @app_commands.command(
        name="settftchannel", description="[Admin] Set the channel for TFT LP updates"
    )
    @app_commands.describe(channel="Channel to post TFT updates in")
    @app_commands.default_permissions(administrator=True)
    async def settftchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"tft_channel_id": channel.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ TFT updates will be posted in {channel.mention}.", ephemeral=True
        )

    # ── /tftlist ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="tftlist",
        description="Show the TFT LP leaderboard for all tracked accounts",
    )
    async def tftlist(self, interaction: discord.Interaction):
        accounts = await self.bot.riot_accounts_col.find(
            {"guild_id": interaction.guild_id}
        ).to_list(length=100)

        if not accounts:
            await interaction.response.send_message(
                "*no accounts are being tracked yet. Use `/registerriot` to add one.*",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        rows = []
        for acc in accounts:
            puuid = acc.get("puuid", "")
            tft = acc.get("tft", {})
            region = tft.get("region") or _val_to_tft_region(
                acc.get("val_region", "eu")
            )
            entries = await self.riot.get_league_entries(region, puuid)
            lp_total = 0
            rank_str = "Unranked"
            for e in entries:
                if e.get("queueType") == "RANKED_TFT":
                    lp_total = _lp_total(e["tier"], e["rank"], e["leaguePoints"])
                    rank_str = _format_rank(e["tier"], e["rank"], e["leaguePoints"])
                    # Update stored LP
                    await self.bot.riot_accounts_col.update_one(
                        {"_id": acc["_id"]},
                        {"$set": {"tft.lp": lp_total, "tft.region": region}},
                    )
            rows.append(
                {
                    "name": acc.get("val_name", "?"),
                    "tag": acc.get("val_tag", "?"),
                    "rank": rank_str,
                    "lp": lp_total,
                }
            )
            await asyncio.sleep(1)

        rows.sort(key=lambda r: r["lp"], reverse=True)
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        embed = discord.Embed(title="🎮 TFT LP Leaderboard", color=0x00B4D8)
        for i, row in enumerate(rows):
            medal = medals.get(i, f"`#{i+1}`")
            embed.add_field(
                name=f"{medal} {row['name']}#{row['tag']}",
                value=row["rank"],
                inline=False,
            )
        embed.set_footer(text=f"Reverie  -  {interaction.guild.name}")
        await interaction.followup.send(embed=embed)

    # ── /tftstats ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="tftstats", description="Show TFT ranked stats for any player"
    )
    @app_commands.describe(
        username="Riot ID including tag, e.g. Name#EUW",
        region="Server region",
    )
    @app_commands.choices(
        region=[
            app_commands.Choice(name="EUW", value="euw1"),
            app_commands.Choice(name="EUNE", value="eun1"),
            app_commands.Choice(name="NA", value="na1"),
            app_commands.Choice(name="AP", value="sg2"),
            app_commands.Choice(name="KR", value="kr"),
            app_commands.Choice(name="BR", value="br1"),
            app_commands.Choice(name="LATAM", value="la1"),
        ]
    )
    async def tftstats(
        self, interaction: discord.Interaction, username: str, region: str = "euw1"
    ):
        if "#" not in username:
            await interaction.response.send_message(
                "⚠️ Include the tag, e.g. `Name#EUW`.", ephemeral=True
            )
            return

        name, tag = username.split("#", 1)
        await interaction.response.defer()

        routing = _region_to_routing(region)
        account = await self.riot.get_account(routing, name, tag)
        if not account:
            await interaction.followup.send(f"⚠️ Couldn't find **{username}**.")
            return

        entries = await self.riot.get_league_entries(region, account["puuid"])
        if not entries:
            await interaction.followup.send(
                f"*no ranked TFT data found for **{username}**.*"
            )
            return

        embed = discord.Embed(title=f"{name}#{tag}  -  TFT Stats", color=0x00B4D8)
        for e in entries:
            queue = (
                e.get("queueType", "").replace("_", " ").title().replace("Tft", "TFT")
            )
            tier = e.get("tier", "").capitalize()
            div = e.get("rank", "")
            lp = e.get("leaguePoints", 0)
            wins = e.get("wins", 0)
            losses = e.get("losses", 0)
            wr = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
            embed.add_field(
                name=queue,
                value=f"**{tier} {div} {lp}LP** - {wins}W {losses}L ({wr}%)",
                inline=False,
            )
        embed.set_footer(text=f"Reverie  -  {interaction.guild.name}")
        await interaction.followup.send(embed=embed)

        # ── Poll task ─────────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def poll_task(self):
        for guild in self.bot.guilds:
            settings = await self.bot.settings_col.find_one({"guild_id": guild.id})
            if not settings or not settings.get("tft_channel_id"):
                continue
            channel = guild.get_channel(settings["tft_channel_id"])
            if not channel:
                continue

            accounts = await self.bot.riot_accounts_col.find(
                {"guild_id": guild.id}
            ).to_list(length=100)

            for account in accounts:
                try:
                    await self._check_account(account, channel)
                except Exception as e:
                    print(f"[TFT] Error checking {account.get('name')}: {e}")
                await asyncio.sleep(2)

    async def _check_account(self, account: dict, channel: discord.TextChannel):
        tft = account.get("tft", {})
        # Use real Riot PUUID for official API calls (Henrik PUUID won't work)
        puuid = account.get("riot_puuid") or account.get("puuid", "")
        region = tft.get("region") or _val_to_tft_region(
            account.get("val_region", "eu")
        )
        routing = _region_to_routing(region)
        name = tft.get("name", "?")
        tag = tft.get("tag", "?")

        # Check for LP change
        entries = await self.riot.get_league_entries(region, puuid)
        new_lp = 0
        tier = div = ""
        raw_lp = 0
        for e in entries:
            if e.get("queueType") == "RANKED_TFT":
                tier = e.get("tier", "")
                div = e.get("rank", "")
                raw_lp = e.get("leaguePoints", 0)
                new_lp = _lp_total(tier, div, raw_lp)

        old_lp = account.get("tft", {}).get("lp", 0)
        lp_diff = new_lp - old_lp

        if lp_diff == 0:
            return
            return

        # Update stored LP
        await self.bot.riot_accounts_col.update_one(
            {"_id": account["_id"]},
            {"$set": {"tft.lp": new_lp}},
        )

        rank_str = _format_rank(tier, div, raw_lp) if tier else "Unranked"
        won = lp_diff > 0
        colour = 0x4FBD6E if won else 0x8B4A4A

        embed = discord.Embed(
            title=f"{name}#{tag}  -  {'WIN' if won else 'LOSS'}",
            color=colour,
        )
        embed.add_field(name="Rank", value=f"**{rank_str}**", inline=True)
        embed.add_field(name="Change", value=f"**{_lp_arrow(lp_diff)}**", inline=True)
        embed.add_field(name="Placement", value="*updating...*", inline=True)
        embed.set_footer(text=f"Reverie  -  {channel.guild.name}")

        msg = await channel.send(embed=embed)

        # Store message ID so we can edit it with placement once match data arrives
        await self.bot.riot_accounts_col.update_one(
            {"_id": account["_id"]},
            {"$set": {"tft.last_message_id": str(msg.id)}},
        )

        # Try to get placement from match history
        await asyncio.sleep(3)
        match_ids = await self.riot.get_match_ids(routing, puuid, count=5)
        known_ids = set(account.get("tft", {}).get("last_match_ids", []))

        for match_id in match_ids:
            if match_id in known_ids:
                continue

            match = await self.riot.get_match(routing, match_id)
            if not match:
                continue
            if match["info"].get("queue_id") != 1100:
                continue

            participants = match["info"].get("participants", [])
            player = next((p for p in participants if p["puuid"] == puuid), None)
            if not player:
                continue

            placement = player.get("placement", "?")
            eliminations = player.get("players_eliminated", 0)
            damage = player.get("total_damage_to_players", 0)
            level = player.get("level", 0)
            tactician_id = player.get("companion", {}).get("item_ID", 0)
            icon_url = await self._get_companion_icon(tactician_id)

            # Edit the embed with full match data
            embed.set_field_at(
                2, name="Placement", value=f"**#{placement}**", inline=True
            )
            embed.add_field(name="Elims", value=f"**{eliminations}**", inline=True)
            embed.add_field(name="Damage", value=f"**{damage}**", inline=True)
            embed.add_field(name="Level", value=f"**{level}**", inline=True)
            if icon_url:
                embed.set_thumbnail(url=icon_url)

            await msg.edit(embed=embed)

            # Update known match IDs (keep last 20)
            new_ids = list(known_ids | {match_id})[-20:]
            await self.bot.riot_accounts_col.update_one(
                {"_id": account["_id"]},
                {"$set": {"tft.last_match_ids": new_ids, "tft.last_message_id": ""}},
            )
            break

    async def _get_companion_icon(self, item_id: int) -> str | None:
        """Fetch companion icon URL, using cache."""
        if not self._companions:
            session = self.riot.session or aiohttp.ClientSession()
            try:
                async with session.get(COMPANIONS_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        for c in data:
                            raw = c.get("loadoutsIcon", "")
                            url = raw.replace(
                                "/lol-game-data/assets/ASSETS/Loadouts/Companions/",
                                "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/assets/loadouts/companions/",
                            ).lower()
                            self._companions[c.get("itemId")] = url
            except Exception:
                pass

        return self._companions.get(item_id)

    @poll_task.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


def _lp_to_rank_str(lp_total: int) -> str:
    """Convert a stored LP total back to a readable rank string."""
    if lp_total == 0:
        return "Unranked"
    for tier, base in sorted(TIER_ORDER.items(), key=lambda x: x[1], reverse=True):
        if lp_total >= base:
            remainder = lp_total - base
            if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
                return f"{tier.capitalize()} {remainder}LP"
            for div, dbase in sorted(
                DIVISION_ORDER.items(), key=lambda x: x[1], reverse=True
            ):
                if remainder >= dbase:
                    return f"{tier.capitalize()} {div} {remainder - dbase}LP"
    return "Unranked"


async def setup(bot: commands.Bot):
    await bot.add_cog(TFTTracker(bot))
