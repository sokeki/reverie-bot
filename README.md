# 🌙 Reverie

A dreamy Discord points bot for **Hypnagogia**. Tracks voice chat time and messages, rewards members with dream points, and includes a fully featured shop, dashboard, guest invite system, anonymous Q&A game, and Valorant/TFT tracking.

---

## Features

### Points and ranks
| Trigger | Reward |
|---|---|
| Every 10 messages sent | 1 dream point |
| Every `VOICE_BLOCK_MINUTES` in voice chat | 1 dream point |

Ranks use an infinite Greek letter system (alpha, beta, gamma... omega, alpha-alpha...) based on combined voice minutes and messages sent. Streaks track consecutive days of activity.

### Shop item types
| Type | Description |
|---|---|
| 🎭 Role | Grants a Discord role on purchase |
| ✨ Title | Equippable title shown on /points |
| 🖊️ Custom Title | One-use item - member types their own title |
| 🗑️ Role Remover | Consumable - removes one purchased shop role |

---

## Commands

### Everyone
| Command | Description |
|---|---|
| `/points` | Check your dream points, voice time, rank and streak |
| `/points @user` | Check another member's stats |
| `/leaderboard` | Hall of Dreamers - sortable by points, rank, voice, messages |
| `/shop` | Browse the dream shop (10 items per page) |
| `/buy <item>` | Purchase an item from the shop |
| `/inventory` | See the items you own |
| `/inventory @user` | Check another member's inventory |
| `/equip` | Equip a purchased role from your inventory via dropdown |
| `/unequip` | Unequip a role you currently have equipped via dropdown |
| `/settitle <title>` | Equip a title to display on your /points profile |
| `/setcustomtitle <text>` | Use a Custom Title item to set your own unique title (max 32 characters) |
| `/rolepreview <item>` | Preview a role colour before buying |
| `/removerole` | Use a Role Remover to remove one of your shop roles |
| `/answer` | Answer today's anonymous question - a modal appears with a random question |
| `/dashboard` | Get the link to the Reverie dashboard |

### Valorant
| Command | Description |
|---|---|
| `/randomagent` | Get a random Valorant agent (optional role filter) |
| `/randomrole` | Get a random Valorant role |
| `/randomcomp` | Assign 5 players to Valorant roles (optional agent roll, results tracked for recap) |
| `/registerriot <name#tag> <region>` | Add a Riot account to server tracking (Valorant RR + TFT LP) |
| `/unregisterriot <name#tag>` | Remove a Riot account from server tracking |
| `/valleaderboard` | See current rank and RR for all registered players, sorted by ELO |
| `/valstats <name#tag> <region> [detail]` | Valorant stats for any player. Optional detail: `clutch`, `utility`, `behaviour`, `agents`, `maps` |
| `/tftleaderboard` | TFT LP leaderboard for all tracked accounts |
| `/tftstats <name#tag> <region>` | TFT ranked stats for any player: wins, losses, winrate and rank |
| `/scoreboard` | Scoreboard for a match - provide a match ID or username, or reply to an RR update with `r!sb` |
| `/footshot <name#tag>` | Headshot, bodyshot and legshot percentages across last 10 competitive games |

### Invite role only
| Command | Description |
|---|---|
| `/guestinvite` | Generate a one-use guest invite (10 min expiry) - guest is moved to your VC on join, kicked when they leave |
| `/drag @member` | Drag a member with the lingering role into your current VC |

### Admin only
| Command | Description |
|---|---|
| `/addpoints @user <amount>` | Add or remove dream points from a member |
| `/additem` | Add an item to the shop |
| `/removeitem <n>` | Remove an item from the shop by name |
| `/edititem <n>` | Edit an existing shop item's name, cost, description or linked role |
| `/setinviterole @role` | Set the role required to generate guest invites and use /drag |
| `/setlingeringrole @role` | Set the role that can be dragged with /drag |
| `/setanswerchannel #channel` | Set the channel where anonymous answers are posted |
| `/setguessingrole @role` | Set the role required to guess in the anonymous Q&A game |
| `/setguesstimeout <hours>` | Set how many hours guessing stays open after an answer is posted |
| `/setanonymouspoints <amount>` | Set the points awarded to the answerer for surviving 3 wrong guesses |
| `/setguesspoints <amount>` | Set the points awarded for a correct guess |
| `/addquestion <text>` | Add a question to the anonymous Q&A pool |
| `/removequestion <text>` | Remove a question from the pool by its exact text |
| `/listquestions` | List all questions in the anonymous Q&A pool |
| `/setrecapchannel #channel` | Set the channel where the weekly recap is posted every Monday at midnight UTC |
| `/sendrecap` | Manually trigger this week's recap |
| `/setvalchannel #channel` | Set the channel for Valorant RR and TFT tracking updates and the daily summary |
| `/valtrackerstatus` | Check if the RR tracker is running and see the status of all registered accounts |
| `/valtrackertest <name#tag>` | Test the Henrik API for a specific account and see the raw response |
| `/settftchannel #channel` | Set the channel for TFT LP tracking updates |

---

## Project structure

```
reverie/
├── bot.py               - main entry point, DB setup, message tracking
├── config.py            - constants and fallback settings
├── Procfile             - Heroku worker + web dynos
├── runtime.txt          - Python version for Heroku
├── requirements.txt     - all dependencies
├── .gitignore
├── cogs/
│   ├── admin.py         - /addpoints, /dashboard
│   ├── anonymous.py     - /answer, anonymous Q&A game with guessing and points
│   ├── guest_invite.py  - /guestinvite, /drag, /setinviterole, /setlingeringrole
│   ├── leaderboard.py   - /leaderboard with sort options
│   ├── mudae_cleaner.py - auto-deletes Mudae roll messages on a delay
│   ├── points.py        - /points embed with rank, streak and comp roll history
│   ├── recap.py         - weekly recap posted every Monday, /setrecapchannel, /sendrecap
│   ├── rr_tracker.py    - Valorant RR tracking, /valstats, /valleaderboard, /scoreboard, /footshot
│   ├── shop.py          - full shop system, /buy, /inventory, /equip, /unequip, /setcustomtitle
│   ├── tft.py           - TFT LP tracking, /tftleaderboard, /tftstats
│   ├── valorant.py      - /randomagent, /randomrole, /randomcomp
│   └── voice.py         - voice tracking, points, persistent sessions
├── utils/
│   ├── db.py            - shared DB helpers
│   ├── ranks.py         - infinite Greek letter rank system
│   └── streaks.py       - streak tracking logic
├── scripts/
│   ├── migrate_accounts.py  - one-off data migration helper
│   ├── reset_accounts.py    - wipe account data for a guild
│   ├── roles.py             - bulk Valorant agent role setup script
│   ├── fix_rr.py            - backfill missing RR history from API
│   ├── recache_games.py     - rebuild cached game data from stored match docs
│   ├── remove_match.py      - remove a specific match from the DB
│   ├── remove_duplicates.py - deduplicate match records
│   └── reset_15Apr.py       - point-in-time reset script
└── dashboard/
    ├── app.py           - FastAPI app, Discord OAuth, all routes
    ├── static/
    │   ├── favicon.svg  - moon tab icon
    │   ├── og-image.png - Open Graph preview image
    │   ├── style.css    - Reverie theme
    │   └── fonts/       - Alter Haas Grotesk font files (add manually)
    └── templates/
        ├── base.html        - shared nav and layout
        ├── login.html       - Discord OAuth login page
        ├── index.html       - overview and server stats
        ├── leaderboard.html - Hall of Dreamers with sort pills
        ├── shop.html        - shop browser and admin CRUD
        ├── commands.html    - command reference
        └── settings.html    - admin settings panel
```

---

## Setup - running locally

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications - **New Application**
2. Go to **Bot** tab - **Reset Token** and copy it
3. Under **Privileged Gateway Intents**, enable:
   - Server Members Intent
   - Message Content Intent
4. Go to **OAuth2 - URL Generator**, select scopes:
   - `bot` and `applications.commands`
   - Permissions: `Send Messages`, `Read Message History`, `Connect`, `View Channels`, `Manage Roles`, `Kick Members`, `Create Instant Invite`, `Move Members`
5. Open the generated URL and invite the bot to your server

> Reverie's bot role must be **above** any purchasable roles in Server Settings - Roles, otherwise it cannot assign or remove them.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in all values
```

### 4. Run

```bash
python bot.py
```

---

## Setup - deploying to Heroku via GitHub

### 1. Push to GitHub

Make sure `.env` is in `.gitignore` and never committed.

### 2. Create a Heroku app

1. Go to https://dashboard.heroku.com - **New - Create new app**
2. Under **Deploy** tab - select **GitHub**
3. Connect your repository and enable **Automatic Deploys**

### 3. Set environment variables

Go to **Settings - Reveal Config Vars** and add:

| Key | Value |
|---|---|
| `BOT_TOKEN` | Your Discord bot token |
| `MONGO_URI` | Your MongoDB Atlas connection string |
| `DB_NAME` | `discord_points` |
| `GUILD_ID` | Your Discord server ID |
| `DISCORD_CLIENT_ID` | Your Discord app client ID |
| `DISCORD_CLIENT_SECRET` | Your Discord app client secret |
| `DISCORD_REDIRECT_URI` | `https://your-app.herokuapp.com/callback` |
| `DASHBOARD_SECRET_KEY` | A long random string for session signing |
| `HENRIK_API_KEY` | Your Henrik API key (for Valorant/TFT tracking) |

### 4. Deploy

Push to GitHub or click **Deploy Branch** in the Heroku dashboard.

### 5. Enable dynos

In Heroku - **Resources** - enable both the `worker` and `web` dynos.

### 6. Check logs

```bash
heroku logs --tail --app your-app-name
```

---

## Configuration

All settings live in `config.py` as fallback defaults. They can be overridden live via the dashboard without restarting:

```python
MESSAGES_PER_POINT     = 10   # messages needed to earn 1 point
POINTS_PER_VOICE_BLOCK = 1    # points awarded per voice block
VOICE_BLOCK_MINUTES    = 30   # how many minutes = 1 block
VOICE_TICK_SECONDS     = VOICE_BLOCK_MINUTES * 60
```

---

## MongoDB collections

| Collection | Purpose |
|---|---|
| `users` | Points, voice minutes, messages, streak per member |
| `shop_items` | Items listed in the shop |
| `inventories` | Items owned by each member, active title |
| `guild_settings` | Live settings, embed colours, invite roles, guest list |
| `voice_sessions` | Persistent voice session times (survives restarts) |
| `anon_rounds` | Anonymous Q&A rounds, answers, guesses and outcomes |
| `comp_rolls` | Valorant comp roll history per member per week |
| `val_matches` | Cached Valorant match data for registered accounts |
| `tft_accounts` | Registered TFT accounts and their last known LP |

---

## Security

- **Never commit your `.env` file.** It is listed in `.gitignore`.
- If your bot token is ever exposed in a commit, regenerate it immediately in the Discord Developer Portal and update the Heroku config var.
- If your MongoDB URI is exposed, rotate the Atlas password immediately under **Database Access**.