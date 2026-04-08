"""
One-time setup script for Valorant agent roles and shop items.

Run this script ONCE from your local machine:
    python scripts/setup_valorant_roles.py

It will:
1. Create Discord roles for each agent with the correct colour
2. Insert shop items into MongoDB with ultimate voicelines as names
   and match-start voicelines as descriptions

Requirements:
- pip install discord.py motor python-dotenv
- .env file with BOT_TOKEN, MONGO_URI, DB_NAME, GUILD_ID
"""

import asyncio
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import discord

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "discord_points")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# ── Agent data ────────────────────────────────────────────────────────────────
# Excluded: Neon, Fade, Iso, Phoenix
# Format: name, hex_colour, enemy_ult_voiceline (role name), match_start_voiceline (description)

AGENTS = [
    # Duelists
    ("Jett", 0xA8B8C8, "Get out of my way!", "Wind's picking up. Let's move."),
    (
        "Reyna",
        0x9060B0,
        "The hunt begins.",
        "They're already dead. They just don't know it.",
    ),
    ("Raze", 0xC07060, "Fire in the hole!", "Let's blow this wide open!"),
    ("Yoru", 0x5060A0, "Who's next?", "They can't stop what they can't see."),
    ("Waylay", 0x70A0B0, "One. By. One.", "Time to move fast. Follow my lead."),
    # Initiators
    ("Sova", 0x508090, "Nowhere to run!", "The hunt is on. Stay sharp."),
    ("Breach", 0xA07050, "Off your feet!", "Ready to make some noise."),
    ("Skye", 0x708060, "I've got your trail!", "Together we move as one."),
    ("KAY/O", 0x808890, "You. Are. Powerless.", "Suppression online. Moving out."),
    (
        "Gekko",
        0x7A9060,
        "Oye! Monster on the loose!",
        "Thrash is ready. Let's go, team!",
    ),
    (
        "Tejo",
        0x807060,
        "Go ahead, stand your ground!",
        "I have eyes on the target. Move in.",
    ),
    # Controllers
    (
        "Brimstone",
        0xB09060,
        "Prepare for hellfire!",
        "Alright, let's move with purpose.",
    ),
    (
        "Viper",
        0x507060,
        "Welcome to my world!",
        "Stay out of my way and we'll be fine.",
    ),
    ("Omen", 0x604870, "Scatter!", "I am the dark they fear."),
    ("Astra", 0x7060A0, "You're divided.", "The stars are aligned. Let us begin."),
    ("Harbor", 0x508080, "I suggest you move.", "The tide is turning. Move with it."),
    (
        "Clove",
        0x906878,
        "You had your fun, my turn!",
        "Death's not the end for me. Let's play.",
    ),
    # Sentinels
    (
        "Sage",
        0x609080,
        "You will not kill my allies!",
        "Stay close. I will keep you standing.",
    ),
    (
        "Cypher",
        0x989080,
        "I know EXACTLY where you are.",
        "Eyes everywhere. They cannot hide.",
    ),
    ("Killjoy", 0xA08840, "You should run!", "Systems online. Let's get to work."),
    (
        "Chamber",
        0xA09070,
        "You want to play? Let's play.",
        "Elegance under pressure. Shall we?",
    ),
    (
        "Deadlock",
        0x6080A0,
        "My territory, my rules!",
        "No one gets through me. Not today.",
    ),
    ("Vyse", 0x708090, "Adapt or die!", "The trap is set. Walk into my web."),
]

ROLE_COST = 250  # dream points per role — adjust as needed


# ── Main ──────────────────────────────────────────────────────────────────────


async def main():
    # Connect to MongoDB
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    items_col = db["shop_items"]

    # Connect to Discord
    intents = discord.Intents.default()
    intents.guilds = True
    client_dc = discord.Client(intents=intents)

    @client_dc.event
    async def on_ready():
        guild = client_dc.get_guild(GUILD_ID)
        if not guild:
            print(f"ERROR: Guild {GUILD_ID} not found. Check your GUILD_ID.")
            await client_dc.close()
            return

        print(f"Connected to guild: {guild.name}")
        print(f"Processing {len(AGENTS)} agents...\n")

        # Fetch existing roles to avoid duplicates
        existing_roles = {r.name: r for r in guild.roles}
        # Fetch existing shop items to avoid duplicates
        existing_items = set()
        async for item in items_col.find({"guild_id": GUILD_ID}):
            existing_items.add(item["name"])

        created_roles = 0
        skipped_roles = 0
        created_items = 0
        skipped_items = 0

        for agent_name, colour_int, ult_line, match_line in AGENTS:
            role_name = ult_line.upper()  # role name in all caps

            # Create Discord role if it doesn't exist
            if role_name in existing_roles:
                role = existing_roles[role_name]
                print(f"  [skip role]  {agent_name} -> '{role_name}' already exists")
                skipped_roles += 1
            else:
                try:
                    role = await guild.create_role(
                        name=role_name,
                        colour=discord.Colour(colour_int),
                        hoist=True,  # display separately from other members
                        reason=f"Valorant agent role: {agent_name}",
                    )
                    print(
                        f"  [created]    {agent_name} -> '{role_name}' (#{colour_int:06X})"
                    )
                    created_roles += 1
                    await asyncio.sleep(0.5)  # rate limit safety
                except discord.Forbidden:
                    print(f"  [error]      No permission to create role '{role_name}'")
                    continue
                except Exception as e:
                    print(f"  [error]      {agent_name}: {e}")
                    continue

            # Insert shop item if it doesn't exist
            colour_hex = f"{colour_int:06X}"
            if role_name in existing_items:
                print(f"  [skip item]  '{role_name}' already in shop")
                skipped_items += 1
            else:
                await items_col.insert_one(
                    {
                        "guild_id": GUILD_ID,
                        "name": role_name,
                        "type": "role",
                        "cost": ROLE_COST,
                        "description": match_line,
                        "role_id": role.id,
                        "role_colour": colour_hex,
                    }
                )
                print(f"  [shop item]  '{ult_line}' added for {ROLE_COST} pts")
                created_items += 1

        print(f"\nDone!")
        print(f"  Roles created:  {created_roles}  (skipped: {skipped_roles})")
        print(f"  Items created:  {created_items}  (skipped: {skipped_items})")
        await client_dc.close()

    await client_dc.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
