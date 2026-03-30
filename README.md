# Discord Points Bot

Tracks voice chat time and messages, awards points, and shows a leaderboard.

---

## Features

| Feature | Command / Trigger |
|---|---|
| Earn points for messages | Automatic (every message = 1 pt) |
| Earn points for voice time | Automatic (every minute in VC = 2 pts) |
| Check your stats | `!points` or `!points @user` |
| Leaderboard | `!leaderboard` / `!lb` / `!top` |
| Admin: give/remove points | `!addpoints @user 50` |

---

## Setup

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications → **New Application**
2. Go to **Bot** tab → **Add Bot**
3. Copy the **Token** (you'll need it for `.env`)
4. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
5. Go to **OAuth2 → URL Generator**, select scopes:
   - `bot`
   - Permissions: `Send Messages`, `Read Message History`, `Connect`, `View Channels`
6. Open the generated URL and invite the bot to your server

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your BOT_TOKEN and MONGO_URI
```

### 4. Run the bot

```bash
python bot.py
```

---

## Configuration (top of bot.py)

```python
POINTS_PER_MSG       = 1   # points per message
POINTS_PER_MIN_VOICE = 2   # points per minute in voice
VOICE_TICK_SECONDS   = 60  # how often voice points are checked
```

Adjust these to balance your economy.

---

## MongoDB

The bot stores all data in a `users` collection with this shape:

```json
{
  "user_id":       123456789,
  "guild_id":      987654321,
  "points":        150,
  "voice_minutes": 45,
  "messages_sent": 60
}
```

You can use **MongoDB Atlas** (free tier) for a cloud database, or run MongoDB locally.
