# 🌙 Reverie

A dreamy Discord points bot for **Hypnogogia**. Tracks voice chat time and messages, rewards members with dream points, and includes a fully featured shop.

---

## Features

### Points
| Trigger | Reward |
|---|---|
| Every 10 messages sent | 1 dream point |
| Every `VOICE_BLOCK_MINUTES` in voice chat | 1 dream point |

### Commands
| Command | Who | Description |
|---|---|---|
| `/points` | Everyone | Check your dream points, voice minutes and messages sent |
| `/points @user` | Everyone | Check another member's stats |
| `/leaderboard` | Everyone | See the Hall of Dreamers — top members by points |
| `/shop` | Everyone | Browse the dream shop (paginated, 5 items per page) |
| `/buy <item>` | Everyone | Purchase an item from the shop |
| `/inventory` | Everyone | See the items you own |
| `/settitle <title>` | Everyone | Equip a title to display on your `/points` profile |
| `/rolepreview <item>` | Everyone | Preview a role's colour before buying |
| `/removerole` | Everyone | Use a Role Remover to remove one of your shop roles |
| `/addpoints @user <amount>` | Admin | Manually add or remove dream points |
| `/additem` | Admin | Add a role, title, or role remover to the shop |
| `/removeitem <name>` | Admin | Remove an item from the shop |
| `/edititem <name>` | Admin | Edit an existing shop item's name, cost, description or role |

### Shop item types
| Type | Description |
|---|---|
| 🎭 Role | Grants a Discord role on purchase |
| ✨ Title | Displays under the member's name in `/points` |
| 🗑️ Role Remover | Consumable — lets the member remove one of their shop roles |

---

## Project structure

```
reverie/
├── bot.py               — startup, DB connection, message tracking, member events
├── config.py            — all settings in one place
├── Procfile             — Heroku worker definition
├── runtime.txt          — Python version for Heroku
├── requirements.txt     — Python dependencies
├── .env.example         — environment variable template
├── .gitignore
├── cogs/
│   ├── points.py        — /points command
│   ├── leaderboard.py   — /leaderboard command
│   ├── admin.py         — /addpoints command
│   ├── voice.py         — voice tracking & background tick
│   └── shop.py          — full shop system
└── utils/
    └── db.py            — shared DB helpers
```

---

## Setup — running locally

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications → **New Application**
2. Go to **Bot** tab → **Reset Token** and copy it
3. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
4. Go to **OAuth2 → URL Generator**, select scopes:
   - `bot` and `applications.commands`
   - Permissions: `Send Messages`, `Read Message History`, `Connect`, `View Channels`, `Manage Roles`
5. Open the generated URL and invite the bot to your server

> ⚠️ Reverie's bot role must be **above** any purchasable roles in Server Settings → Roles, otherwise it can't assign or remove them.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in BOT_TOKEN, MONGO_URI, and DB_NAME
```

### 4. Run

```bash
python bot.py
```

---

## Setup — deploying to Heroku via GitHub

### 1. Push to GitHub

Make sure your `.env` is in `.gitignore` and never committed. Push your code to a GitHub repository.

### 2. Create a Heroku app

1. Go to https://dashboard.heroku.com → **New → Create new app**
2. Under the **Deploy** tab → **Deployment method** → select **GitHub**
3. Connect your repository and enable **Automatic Deploys** if desired

### 3. Set environment variables

Go to **Settings → Reveal Config Vars** and add:

| Key | Value |
|---|---|
| `BOT_TOKEN` | Your Discord bot token |
| `MONGO_URI` | Your MongoDB Atlas connection string |
| `DB_NAME` | `discord_points` |

### 4. Deploy

Either push to GitHub (if auto-deploy is on) or click **Deploy Branch** in the Heroku dashboard.

### 5. Start the worker

In the Heroku dashboard go to **Resources** and enable the `worker` dyno. Or via CLI:

```bash
heroku ps:scale worker=1 --app your-app-name
```

### 6. Check logs

```bash
heroku logs --tail --app your-app-name
```

---

## Configuration

All settings live in `config.py`:

```python
MESSAGES_PER_POINT     = 10   # messages needed to earn 1 point
POINTS_PER_VOICE_BLOCK = 1    # points awarded per voice block
VOICE_BLOCK_MINUTES    = 30   # how many minutes = 1 block  ← change this!
VOICE_TICK_SECONDS     = VOICE_BLOCK_MINUTES * 60
```

---

## MongoDB collections

| Collection | Purpose |
|---|---|
| `users` | Points, voice minutes, messages per member |
| `shop_items` | Items listed in the shop |
| `inventories` | Items owned by each member, active title |

---

## Security

- **Never commit your `.env` file.** It is listed in `.gitignore`.
- If your bot token is ever exposed in a commit, regenerate it immediately in the Discord Developer Portal and update the Heroku config var.
- If your MongoDB URI is exposed, rotate the Atlas password immediately under **Database Access**.