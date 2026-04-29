import os
from datetime import datetime, timedelta
import httpx
from urllib.parse import quote
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "discord_points")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv(
    "DISCORD_REDIRECT_URI"
)  # e.g. https://yourapp.herokuapp.com/callback
SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", "change-me-in-production")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.getenv("BOT_TOKEN")


async def fetch_guild_name() -> str:
    """Fetch the guild name from Discord using the bot token."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{DISCORD_API}/guilds/{GUILD_ID}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
        )
        data = r.json()
        return data.get("name", "Hypnagogia")


async def fetch_guild_roles() -> list[dict]:
    """Fetch all roles for the guild using the bot token."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{DISCORD_API}/guilds/{GUILD_ID}/roles",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
        )
        roles = r.json()
        if not isinstance(roles, list):
            return []
        # Sort by position descending, exclude @everyone
        roles = [r for r in roles if r["name"] != "@everyone"]
        roles.sort(key=lambda r: r.get("position", 0), reverse=True)
        # Add hex colour string
        for role in roles:
            colour_val = role.get("color", 0)
            role["colour_hex"] = f"{colour_val:06X}" if colour_val else None
        return roles


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")
templates = Jinja2Templates(directory="dashboard/templates")

GUILD_NAME = "Hypnagogia"  # overwritten at startup


@app.on_event("startup")
async def startup():
    global GUILD_NAME
    try:
        GUILD_NAME = await fetch_guild_name()
    except Exception:
        pass


templates.env.globals["enumerate"] = enumerate

# ── DB ────────────────────────────────────────────────────────────────────────
_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[DB_NAME]
users_col = _db["users"]
items_col = _db["shop_items"]
settings_col = _db["guild_settings"]
questions_col = _db["questions"]
daily_snapshots_col = _db["daily_snapshots"]


async def get_settings() -> dict:
    doc = await settings_col.find_one({"guild_id": GUILD_ID})
    defaults = {
        "guild_id": GUILD_ID,
        "messages_per_point": 10,
        "voice_block_minutes": 30,
        "points_per_voice_block": 1,
        "colour_main": "9b8ec4",
        "colour_lb": "6a5acd",
        "colour_confirm": "b8a9d9",
    }
    if doc:
        defaults.update({k: v for k, v in doc.items() if k != "_id"})
    return defaults


# ── Auth helpers ──────────────────────────────────────────────────────────────


async def get_current_user(request: Request) -> dict | None:
    return request.session.get("user")


async def require_user(request: Request) -> dict:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


async def require_admin(request: Request) -> dict:
    user = await require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


async def fetch_discord(token: str, path: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.json()


async def is_guild_admin(token: str) -> bool:
    """Check if the user has Administrator permission in the guild."""
    try:
        member = await fetch_discord(token, f"/users/@me/guilds/{GUILD_ID}/member")
        roles = member.get("roles", [])
        # Also check via guild member roles for administrator flag
        # We check the permissions field directly
        perms = int(member.get("permissions", "0"))
        return bool(perms & 0x8)  # 0x8 = ADMINISTRATOR
    except Exception:
        return False


# ── OAuth routes ──────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/auth")
async def auth():
    oauth_url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={quote(DISCORD_REDIRECT_URI, safe='')}"
        "&response_type=code"
        "&scope=identify+guilds.members.read"
    )
    return RedirectResponse(oauth_url, status_code=302)


@app.get("/callback")
async def callback(request: Request, code: str = None, error: str = None):
    # Discord sends ?error= if the user denied access
    if error:
        return RedirectResponse("/login")

    if not code:
        raise HTTPException(status_code=400, detail="No code received")

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": DISCORD_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        token_data = token_resp.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail=f"OAuth failed: {token_data}")

        user = await fetch_discord(access_token, "/users/@me")

        is_admin = await is_guild_admin(access_token)

        request.session["user"] = {
            "id": user["id"],
            "username": user["username"],
            "avatar": user.get("avatar"),
            "is_admin": is_admin,
        }
        return RedirectResponse("/", status_code=302)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/avatar-proxy")
async def avatar_proxy(url: str):
    """Proxy Discord avatar images to avoid CORB issues."""
    import httpx
    from fastapi.responses import Response

    if not url.startswith("https://cdn.discordapp.com/"):
        raise HTTPException(status_code=400)
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
    return Response(
        content=r.content, media_type=r.headers.get("content-type", "image/png")
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# ── Pages ─────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(require_user)):
    # Server stats - single aggregation for efficiency
    total_members = await users_col.count_documents({"guild_id": GUILD_ID})
    total_items = await items_col.count_documents({"guild_id": GUILD_ID})
    settings = await get_settings()

    agg = await users_col.aggregate(
        [
            {"$match": {"guild_id": GUILD_ID}},
            {
                "$group": {
                    "_id": None,
                    "total_points": {"$sum": "$points"},
                    "total_voice": {"$sum": "$voice_minutes"},
                    "total_messages": {"$sum": "$messages_sent"},
                }
            },
        ]
    ).to_list(length=1)

    total_points = agg[0]["total_points"] if agg else 0
    total_voice = agg[0]["total_voice"] if agg else 0
    total_messages = agg[0]["total_messages"] if agg else 0

    # Activity data for overview charts
    daily_docs = (
        await daily_snapshots_col.find({"guild_id": GUILD_ID, "type": "server"})
        .sort("date", -1)
        .limit(30)
        .to_list(length=30)
    )
    daily_docs.reverse()
    daily_chart = []
    for i, doc in enumerate(daily_docs):
        if i == 0:
            continue
        prev = daily_docs[i - 1]
        _d = doc.get("date", "")
        if _d:
            _d = (datetime.strptime(_d, "%Y-%m-%d") - timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
        daily_chart.append(
            {
                "date": _d,
                "points": max(0, doc.get("points", 0) - prev.get("points", 0)),
                "voice": max(0, doc.get("voice", 0) - prev.get("voice", 0)),
                "messages": max(0, doc.get("messages", 0) - prev.get("messages", 0)),
            }
        )

    weekly_pipeline = [
        {"$match": {"guild_id": GUILD_ID}},
        {
            "$group": {
                "_id": "$week",
                "points": {"$sum": "$points"},
                "voice_minutes": {"$sum": "$voice_minutes"},
                "messages_sent": {"$sum": "$messages_sent"},
            }
        },
        {"$sort": {"_id": 1}},
        {"$limit": 12},
    ]
    weekly_agg = (
        await _db["weekly_snapshots"].aggregate(weekly_pipeline).to_list(length=12)
    )
    raw_weekly = [
        {
            "week": d["_id"],
            "points": d["points"],
            "voice_minutes": d["voice_minutes"],
            "messages_sent": d["messages_sent"],
        }
        for d in weekly_agg
    ]
    weekly_chart = []
    for i, row in enumerate(raw_weekly):
        if i == 0:
            continue
        prev = raw_weekly[i - 1]
        weekly_chart.append(
            {
                "week": row["week"],
                "points": max(0, row["points"] - prev["points"]),
                "voice_minutes": max(0, row["voice_minutes"] - prev["voice_minutes"]),
                "messages_sent": max(0, row["messages_sent"] - prev["messages_sent"]),
            }
        )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
            "total_members": total_members,
            "total_points": total_points,
            "total_voice": total_voice,
            "total_messages": total_messages,
            "total_items": total_items,
            "settings": settings,
            "daily": daily_chart,
            "weekly": weekly_chart,
        },
    )


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request, user: dict = Depends(require_user)):
    sort_param = request.query_params.get("sort", "points")
    sort_map = {
        "points": "points",
        "rank": "voice_minutes",  # activity score proxy
        "voice": "voice_minutes",
        "messages": "messages_sent",
    }
    db_field = sort_map.get(sort_param, "points")

    docs = (
        await users_col.find({"guild_id": GUILD_ID})
        .sort(db_field, -1)
        .limit(25)
        .to_list(length=25)
    )

    # For rank, sort by combined activity score
    if sort_param == "rank":
        docs.sort(
            key=lambda d: d.get("voice_minutes", 0) + d.get("messages_sent", 0),
            reverse=True,
        )

    # Add computed rank and formatted voice time to each doc
    from utils.ranks import get_rank as _get_rank

    for doc in docs:
        score = doc.get("voice_minutes", 0) + doc.get("messages_sent", 0)
        r = _get_rank(score)
        doc["rank_symbol"] = r["symbol"]
        doc["rank_name"] = r["name"]
        mins = doc.get("voice_minutes", 0)
        h, m = divmod(mins, 60)
        doc["voice_fmt"] = f"{h}h {m}m" if h else f"{m}m"

    # Bar chart data (top 10 by each stat)
    top_pts = (
        await users_col.find(
            {"guild_id": GUILD_ID}, {"username": 1, "user_id": 1, "points": 1}
        )
        .sort("points", -1)
        .limit(10)
        .to_list(length=10)
    )
    top_voice = (
        await users_col.find(
            {"guild_id": GUILD_ID}, {"username": 1, "user_id": 1, "voice_minutes": 1}
        )
        .sort("voice_minutes", -1)
        .limit(10)
        .to_list(length=10)
    )
    top_msgs = (
        await users_col.find(
            {"guild_id": GUILD_ID}, {"username": 1, "user_id": 1, "messages_sent": 1}
        )
        .sort("messages_sent", -1)
        .limit(10)
        .to_list(length=10)
    )

    def _lbl(d):
        name = d.get("username") or ""
        if name.strip():
            return name
        uid = d.get("user_id")
        return f"#{str(uid)[-4:]}" if uid else "?"

    # Server weekly activity
    weekly_pipeline = [
        {"$match": {"guild_id": GUILD_ID}},
        {
            "$group": {
                "_id": "$week",
                "points": {"$sum": "$points"},
                "voice_minutes": {"$sum": "$voice_minutes"},
                "messages_sent": {"$sum": "$messages_sent"},
            }
        },
        {"$sort": {"_id": 1}},
        {"$limit": 12},
    ]
    weekly_agg = (
        await _db["weekly_snapshots"].aggregate(weekly_pipeline).to_list(length=12)
    )
    # Compute server-wide deltas week-over-week
    raw = [
        {
            "week": d["_id"],
            "points": d["points"],
            "voice_minutes": d["voice_minutes"],
            "messages_sent": d["messages_sent"],
        }
        for d in weekly_agg
    ]
    # Skip first entry — no reference point for delta
    weekly = []
    for i, row in enumerate(raw):
        if i == 0:
            continue
        else:
            prev = raw[i - 1]
            weekly.append(
                {
                    "week": row["week"],
                    "points": max(0, row["points"] - prev["points"]),
                    "voice_minutes": max(
                        0, row["voice_minutes"] - prev["voice_minutes"]
                    ),
                    "messages_sent": max(
                        0, row["messages_sent"] - prev["messages_sent"]
                    ),
                }
            )

    return templates.TemplateResponse(
        "leaderboard.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
            "members": docs,
            "sort": sort_param,
            "bar_points_labels": [_lbl(d) for d in top_pts],
            "bar_points_values": [d.get("points", 0) for d in top_pts],
            "bar_voice_labels": [_lbl(d) for d in top_voice],
            "bar_voice_values": [d.get("voice_minutes", 0) for d in top_voice],
            "bar_msgs_labels": [_lbl(d) for d in top_msgs],
            "bar_msgs_values": [d.get("messages_sent", 0) for d in top_msgs],
            "weekly": weekly,
        },
    )


@app.get("/shop", response_class=HTMLResponse)
async def shop(request: Request, user: dict = Depends(require_user)):
    items = (
        await items_col.find({"guild_id": GUILD_ID})
        .sort("cost", -1)
        .to_list(length=100)
    )
    for item in items:
        item["_id"] = str(item["_id"])
    # Fetch guild roles for admin dropdowns
    guild_roles = await fetch_guild_roles() if user.get("is_admin") else []
    return templates.TemplateResponse(
        "shop.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
            "items": items,
            "guild_roles": guild_roles,
            "saved": request.query_params.get("saved"),
            "deleted": request.query_params.get("deleted"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/shop/add")
async def shop_add(request: Request, user: dict = Depends(require_admin)):
    from bson import ObjectId

    form = await request.form()
    name = form.get("name", "").strip()
    item_type = form.get("type", "title")
    cost = int(form.get("cost", 0))
    desc = form.get("description", "no description").strip()

    if not name:
        return RedirectResponse("/shop?error=Name+is+required", status_code=303)

    existing = await items_col.find_one(
        {"guild_id": GUILD_ID, "name": {"$regex": f"^{name}$", "$options": "i"}}
    )
    if existing:
        return RedirectResponse(
            "/shop?error=An+item+with+that+name+already+exists", status_code=303
        )

    doc = {
        "guild_id": GUILD_ID,
        "name": name,
        "type": item_type,
        "cost": cost,
        "description": desc,
    }
    if item_type == "role":
        role_id_str = form.get("role_id", "").strip()
        if not role_id_str:
            return RedirectResponse("/shop?error=Please+select+a+role", status_code=303)
        doc["role_id"] = int(role_id_str)
        # Pull colour from Discord directly
        roles = await fetch_guild_roles()
        matched = next((r for r in roles if str(r["id"]) == role_id_str), None)
        doc["role_colour"] = matched["colour_hex"] if matched else None

    await items_col.insert_one(doc)
    return RedirectResponse("/shop?saved=1", status_code=303)


@app.post("/shop/edit/{item_id}")
async def shop_edit(
    item_id: str, request: Request, user: dict = Depends(require_admin)
):
    from bson import ObjectId

    form = await request.form()
    changes = {}
    if form.get("name"):
        changes["name"] = form.get("name").strip()
    if form.get("cost"):
        changes["cost"] = int(form.get("cost"))
    if form.get("description"):
        changes["description"] = form.get("description").strip()
    if form.get("role_colour") is not None:
        changes["role_colour"] = form.get("role_colour").lstrip("#").strip() or None
    if form.get("role_id"):
        role_id_str = form.get("role_id").strip()
        changes["role_id"] = int(role_id_str)
        roles = await fetch_guild_roles()
        matched = next((r for r in roles if str(r["id"]) == role_id_str), None)
        if matched:
            changes["role_colour"] = matched["colour_hex"]

    if changes:
        await items_col.update_one({"_id": ObjectId(item_id)}, {"$set": changes})
    return RedirectResponse("/shop?saved=1", status_code=303)


@app.post("/shop/delete/{item_id}")
async def shop_delete(
    item_id: str, request: Request, user: dict = Depends(require_admin)
):
    from bson import ObjectId

    await items_col.delete_one({"_id": ObjectId(item_id)})
    return RedirectResponse("/shop?deleted=1", status_code=303)


@app.get("/commands", response_class=HTMLResponse)
async def commands_page(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        "commands.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(require_admin)):
    settings = await get_settings()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
            "settings": settings,
            "saved": request.query_params.get("saved"),
        },
    )


@app.post("/settings")
async def save_settings(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    await settings_col.update_one(
        {"guild_id": GUILD_ID},
        {
            "$set": {
                "guild_id": GUILD_ID,
                "messages_per_point": int(form.get("messages_per_point", 10)),
                "voice_block_minutes": int(form.get("voice_block_minutes", 30)),
                "points_per_voice_block": int(form.get("points_per_voice_block", 1)),
                "colour_main": form.get("colour_main", "9b8ec4").lstrip("#"),
                "colour_lb": form.get("colour_lb", "6a5acd").lstrip("#"),
                "colour_confirm": form.get("colour_confirm", "b8a9d9").lstrip("#"),
            }
        },
        upsert=True,
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Questions manager ──────────────────────────────────────────────────────────


@app.get("/questions", response_class=HTMLResponse)
async def questions_page(request: Request, user: dict = Depends(require_admin)):
    questions = (
        await questions_col.find({"guild_id": GUILD_ID})
        .sort("_id", -1)
        .to_list(length=500)
    )
    for q in questions:
        q["_id"] = str(q["_id"])
    return templates.TemplateResponse(
        "questions.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
            "questions": questions,
            "saved": request.query_params.get("saved"),
            "deleted": request.query_params.get("deleted"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/questions/add")
async def questions_add(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    text = form.get("text", "").strip()
    if not text:
        return RedirectResponse(
            "/questions?error=Question+text+is+required", status_code=303
        )
    existing = await questions_col.find_one({"guild_id": GUILD_ID, "text": text})
    if existing:
        return RedirectResponse(
            "/questions?error=That+question+already+exists", status_code=303
        )
    await questions_col.insert_one({"guild_id": GUILD_ID, "text": text})
    return RedirectResponse("/questions?saved=1", status_code=303)


@app.post("/questions/delete/{question_id}")
async def questions_delete(
    question_id: str, request: Request, user: dict = Depends(require_admin)
):
    from bson import ObjectId

    await questions_col.delete_one({"_id": ObjectId(question_id)})
    return RedirectResponse("/questions?deleted=1", status_code=303)


# ── Per-member stats ───────────────────────────────────────────────────────────


@app.get("/member/{user_id}", response_class=HTMLResponse)
async def member_page(
    user_id: int, request: Request, user: dict = Depends(require_user)
):
    member_doc = await users_col.find_one({"guild_id": GUILD_ID, "user_id": user_id})
    if not member_doc:
        raise HTTPException(status_code=404, detail="Member not found")

    # Daily history for this member
    history = (
        await daily_snapshots_col.find(
            {"guild_id": GUILD_ID, "type": "member", "user_id": user_id}
        )
        .sort("date", -1)
        .limit(30)
        .to_list(length=30)
    )
    history.reverse()

    # Always fetch weekly history (for toggle)
    weekly_history = (
        await _db["weekly_snapshots"]
        .find({"guild_id": GUILD_ID, "user_id": user_id})
        .sort("week", 1)
        .limit(12)
        .to_list(length=12)
    )
    if not weekly_history:
        weekly_history = (
            await _db["weekly_snapshots"]
            .find({"user_id": user_id})
            .sort("week", 1)
            .limit(12)
            .to_list(length=12)
        )

    member_doc["_id"] = str(member_doc["_id"])
    for doc in history:
        doc["_id"] = str(doc["_id"])

    # Build daily delta history — skip first entry, no reference point
    daily_chart = []
    for i, doc in enumerate(history):
        if i == 0:
            continue
        prev = history[i - 1]
        daily_chart.append(
            {
                "date": (
                    (
                        datetime.strptime(doc.get("date", ""), "%Y-%m-%d")
                        - timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                    if doc.get("date")
                    else ""
                ),
                "points": max(0, doc.get("points", 0) - prev.get("points", 0)),
                "voice": max(0, doc.get("voice", 0) - prev.get("voice", 0)),
                "messages": max(0, doc.get("messages", 0) - prev.get("messages", 0)),
            }
        )

    # Build weekly delta history for charting
    # Skip first entry — no reference point for delta
    weekly_chart = []
    for i, doc in enumerate(weekly_history):
        if i == 0:
            continue
        prev = weekly_history[i - 1]
        weekly_chart.append(
            {
                "week": doc["week"],
                "points": max(0, doc.get("points", 0) - prev.get("points", 0)),
                "voice_minutes": max(
                    0, doc.get("voice_minutes", 0) - prev.get("voice_minutes", 0)
                ),
                "messages_sent": max(
                    0, doc.get("messages_sent", 0) - prev.get("messages_sent", 0)
                ),
            }
        )

    # Always pass both — toggle in template
    return templates.TemplateResponse(
        "member.html",
        {
            "request": request,
            "guild_name": GUILD_NAME,
            "user": user,
            "member": member_doc,
            "history": daily_chart,
            "weekly_history": weekly_chart,
            "has_daily": len(daily_chart) > 0,
            "has_weekly": len(weekly_chart) > 0,
        },
    )
