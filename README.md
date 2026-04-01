# 🌙 Reverie

A dreamy Discord points bot for **Hypnagogia**. Tracks voice chat time and messages, rewards members with dream points, and includes a fully featured shop, dashboard, and guest invite system.

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
| `/shop` | Browse the dream shop (5 items per page) |
| `/buy <item>` | Purchase an item from the shop |
| `/inventory` | See the items you own |
| `/settitle <title>` | Equip a title to display on your /points profile |
| `/setcustomtitle <text>` | Use a custom title item to set your own title |
| `/rolepreview <item>` | Preview a role colour before buying |
| `/removerole` | Use a Role Remover to remove one of your shop roles |
| `/randomagent` | Get a random Valorant agent (optional role filter) |
| `/randomrole` | Get a random Valorant role |
| `/randomcomp` | Assign 5 players to Valorant roles (optional agent roll) |
| `/dashboard` | Get the link to the Reverie dashboard |

### Invite role only
| Command | Description |
|---|---|
| `/guestinvite` | Generate a one-use guest invite - guest is moved to your VC on join |
| `/drag @member` | Drag a member with the lingering role into your VC |

### Admin only
| Command | Description |
|---|---|
| `/addpoints @user <amount>` | Add or remove dream points from a member |
| `/additem` | Add an item to the shop |
| `/removeitem <name>` | Remove an item from the shop |
| `/edititem <name>` | Edit an existing shop item |
| `/setinviterole @role` | Set the role required to generate guest invites and drag members |
| `/setlingeringrole @role` | Set the role that can be dragged with /drag |
| `/dashboard` | Get the link to the Reverie dashboard |

---

## Project structure

```
discord_points_bot/
├── bot.py               - main entry point, DB setup, message tracking
├── config.py            - constants and fallback settings
├── Procfile             - Heroku worker + web dynos
├── runtime.txt          - Python version for Heroku
├── requirements.txt     - all dependencies
├── .env.example         - environment variable template
├── .gitignore
├── cogs/
│   ├── admin.py         - /addpoints, /dashboard
│   ├── guest_invite.py  - /guestinvite, /drag, /setinviterole, /setlingeringrole
│   ├── leaderboard.py   - /leaderboard with sort options
│   ├── points.py        - /points embed with rank and streak
│   ├── shop.py          - full shop system and /setcustomtitle
│   ├── valorant.py      - /randomagent, /randomrole, /randomcomp
│   └── voice.py         - voice tracking, points, persistent sessions
├── utils/
│   ├── db.py            - shared DB helpers
│   ├── ranks.py         - infinite Greek letter rank system
│   └── streaks.py       - streak tracking logic
└── dashboard/
    ├── app.py           - FastAPI app, Discord OAuth, all routes
    ├── static/
    │   ├── favicon.svg  - moon tab icon
    │   ├── og-image.png - Open Graph preview image
    │   ├── style.css    - Reverie theme
    │   └── fonts/       - Alter Haas Grotesk font files (add manually)
    └── templates/
        ├── base.html    - shared nav and layout
        ├── login.html   - Discord OAuth login page
        ├── index.html   - overview and server stats
        ├── leaderboard.html - Hall of Dreamers with sort pills
        ├── shop.html    - shop browser and admin CRUD
        ├── commands.html - command reference
        └── settings.html - admin settings panel
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

---

## Security

- **Never commit your `.env` file.** It is listed in `.gitignore`.
- If your bot token is ever exposed in a commit, regenerate it immediately in the Discord Developer Portal and update the Heroku config var.
- If your MongoDB URI is exposed, rotate the Atlas password immediately under **Database Access**.