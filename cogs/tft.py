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

from config import COLOUR_MAIN, COLOUR_LB

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
                print(f"[TFT] API {resp.status} for {url}")
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
        name="tftleaderboard",
        description="Show the TFT LP leaderboard for all tracked accounts",
    )
    async def tftleaderboard(self, interaction: discord.Interaction):
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
            tft = acc.get("tft", {})
            puuid = acc.get("riot_puuid") or acc.get("puuid", "")
            region = tft.get("region") or _val_to_tft_region(
                acc.get("val_region", "eu")
            )
            entries = await self.riot.get_league_entries(region, puuid)
            lp_total = tft.get("lp", 0) or 0
            rank_str = "Unranked"
            for e in entries:
                if e.get("queueType") == "RANKED_TFT":
                    lp_total = _lp_total(e["tier"], e["rank"], e["leaguePoints"])
                    rank_str = _format_rank(e["tier"], e["rank"], e["leaguePoints"])
                    await self.bot.riot_accounts_col.update_one(
                        {"_id": acc["_id"]},
                        {"$set": {"tft.lp": lp_total, "tft.region": region}},
                    )
            # If API returned no entries, reset stored LP (new season/unranked)
            if not entries:
                lp_total = 0
                await self.bot.riot_accounts_col.update_one(
                    {"_id": acc["_id"]},
                    {"$set": {"tft.lp": None}},
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
        lines = []
        for i, row in enumerate(rows):
            medal = medals.get(i, f"`#{i+1}`")
            lines.append(f"{medal} **{row['name']}#{row['tag']}** - {row['rank']}")
        embed = discord.Embed(
            title="🎮 TFT LP Leaderboard",
            description="\n".join(lines),
            color=COLOUR_LB,
        )
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
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

        embed = discord.Embed(title=f"{name}#{tag}  -  TFT Stats", color=COLOUR_MAIN)
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
        embed.set_footer(text=f"Reverie  •  {interaction.guild.name}")
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
                    import traceback

                    print(
                        f"[TFT] Error checking {account.get('val_name')}#{account.get('val_tag')}: {e}"
                    )
                    traceback.print_exc()
                await asyncio.sleep(2)

    async def _check_account(self, account: dict, channel: discord.TextChannel):
        tft = account.get("tft", {})
        puuid = account.get("riot_puuid") or account.get("puuid", "")
        region = tft.get("region") or _val_to_tft_region(
            account.get("val_region", "eu")
        )
        routing = _region_to_routing(region)
        name = tft.get("name") or account.get("val_name", "?")
        tag = tft.get("tag") or account.get("val_tag", "?")

        old_lp = tft.get("lp")
        known_ids = set(tft.get("last_match_ids", []))
        baselined = tft.get("baselined", False)
        last_msg_id = tft.get("last_message_id", "")

        # ── Baseline on first run ─────────────────────────────────────────────
        if not baselined:
            match_ids = await self.riot.get_match_ids(routing, puuid, count=10)
            entries = await self.riot.get_league_entries(region, puuid)
            new_lp = 0
            for e in entries:
                if e.get("queueType") == "RANKED_TFT":
                    new_lp = _lp_total(e["tier"], e["rank"], e["leaguePoints"])
            await self.bot.riot_accounts_col.update_one(
                {"_id": account["_id"]},
                {
                    "$set": {
                        "tft.lp": new_lp or None,
                        "tft.last_match_ids": list(match_ids),
                        "tft.baselined": True,
                    }
                },
            )
            print(f"[TFT] Baseline stored for {name}#{tag}")
            return

        # ── Phase 1: check for new matches ────────────────────────────────────
        match_ids = await self.riot.get_match_ids(routing, puuid, count=5)
        new_match_id = None
        new_match_data = None
        for mid in match_ids:
            if mid in known_ids:
                continue
            match = await self.riot.get_match(routing, mid)
            if not match:
                continue
            if match["info"].get("queue_id") != 1100:
                known_ids.add(mid)
                continue
            new_match_id = mid
            new_match_data = match
            break

        # ── Phase 2: check for LP change ─────────────────────────────────────
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

        # Reset LP on new season
        if not entries and old_lp is not None and old_lp > 0:
            await self.bot.riot_accounts_col.update_one(
                {"_id": account["_id"]},
                {"$set": {"tft.lp": None}},
            )
            print(f"[TFT] Season reset for {name}#{tag}, LP cleared")

        old_lp_val = old_lp if old_lp is not None else 0
        lp_diff = new_lp - old_lp_val if new_lp else 0
        lp_changed = bool(new_lp and new_lp != old_lp_val)

        # ── Case 1: LP changed → post embed with pending placement ───────────
        if lp_changed:
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
            embed.add_field(
                name="Change", value=f"**{_lp_arrow(lp_diff)}**", inline=True
            )
            embed.add_field(name="Placement", value="*updating...*", inline=True)
            embed.set_footer(text=f"Reverie  •  {channel.guild.name}")
            msg = await channel.send(embed=embed)
            await self.bot.riot_accounts_col.update_one(
                {"_id": account["_id"]},
                {"$set": {"tft.last_message_id": str(msg.id)}},
            )
            last_msg_id = str(msg.id)

        # ── Case 2: new match found ───────────────────────────────────────────
        if new_match_id and new_match_data:
            participants = new_match_data["info"].get("participants", [])
            player = next((p for p in participants if p["puuid"] == puuid), None)
            if not player:
                return

            placement = player.get("placement", "?")
            eliminations = player.get("players_eliminated", 0)
            damage = player.get("total_damage_to_players", 0)
            level = player.get("level", 0)
            tactician_id = player.get("companion", {}).get("item_ID", 0)
            icon_url = await self._get_companion_icon(tactician_id)

            new_ids = list((known_ids | {new_match_id}))[-20:]

            if last_msg_id:
                # Edit existing LP embed with placement data
                try:
                    msg = await channel.fetch_message(int(last_msg_id))
                    embed = msg.embeds[0]
                    embed.set_field_at(
                        2, name="Placement", value=f"**#{placement}**", inline=True
                    )
                    embed.add_field(
                        name="Elims", value=f"**{eliminations}**", inline=True
                    )
                    embed.add_field(name="Damage", value=f"**{damage}**", inline=True)
                    embed.add_field(name="Level", value=f"**{level}**", inline=True)
                    if icon_url:
                        embed.set_thumbnail(url=icon_url)
                    await msg.edit(embed=embed)
                    print(f"[TFT] Edited embed for {name}#{tag}: #{placement}")
                except Exception as e:
                    print(f"[TFT] Failed to edit embed for {name}#{tag}: {e}")
            else:
                # No pending LP embed — send fresh embed (placements / LP not yet updated)
                won = isinstance(placement, int) and placement <= 4
                colour = 0x4FBD6E if won else 0x8B4A4A
                rank_str = _format_rank(tier, div, raw_lp) if tier else "Placement"
                embed = discord.Embed(
                    title=f"{name}#{tag}  -  {'WIN' if won else 'LOSS'}",
                    color=colour,
                )
                embed.add_field(name="Rank", value=f"**{rank_str}**", inline=True)
                embed.add_field(name="Change", value="**-**", inline=True)
                embed.add_field(
                    name="Placement", value=f"**#{placement}**", inline=True
                )
                embed.add_field(name="Elims", value=f"**{eliminations}**", inline=True)
                embed.add_field(name="Damage", value=f"**{damage}**", inline=True)
                embed.add_field(name="Level", value=f"**{level}**", inline=True)
                if icon_url:
                    embed.set_thumbnail(url=icon_url)
                embed.set_footer(text=f"Reverie  •  {channel.guild.name}")
                await channel.send(embed=embed)
                print(f"[TFT] New match embed for {name}#{tag}: #{placement}")

            await self.bot.riot_accounts_col.update_one(
                {"_id": account["_id"]},
                {"$set": {"tft.last_match_ids": new_ids, "tft.last_message_id": ""}},
            )
            return

        if not lp_changed and not new_match_id:
            return

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


async def setup(bot: commands.Bot):
    await bot.add_cog(TFTTracker(bot))
