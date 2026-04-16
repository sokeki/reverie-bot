"""
Streak tracking utility.

A streak increments when a member is active on a calendar day (UTC) they
weren't already active on. Missing a day resets the streak to 1 (the current
day counts as day 1 of a new streak).

The users collection stores:
  streak:          int  - current streak in days
  streak_best:     int  - all-time longest streak
  streak_last_date: str - last active date as "YYYY-MM-DD" (UTC)
"""

from datetime import datetime, timezone


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


async def record_activity(users_col, user_id: int, guild_id: int) -> dict | None:
    """
    Record today's activity for a member and update their streak.
    Returns the updated streak doc fragment, or None if already recorded today.
    """
    today = _today()

    doc = await users_col.find_one(
        {"user_id": user_id, "guild_id": guild_id},
        {"streak": 1, "streak_best": 1, "streak_last_date": 1},
    )
    if not doc:
        return None

    last_date = doc.get("streak_last_date")

    # Already recorded activity today - nothing to do
    if last_date == today:
        return None

    current_streak = doc.get("streak", 0)
    best_streak = doc.get("streak_best", 0)

    # Was active yesterday → extend streak, otherwise start fresh
    if last_date == _yesterday():
        new_streak = current_streak + 1
    else:
        new_streak = 1

    new_best = max(best_streak, new_streak)

    await users_col.update_one(
        {"user_id": user_id, "guild_id": guild_id},
        {
            "$set": {
                "streak": new_streak,
                "streak_best": new_best,
                "streak_last_date": today,
            }
        },
    )

    return {"streak": new_streak, "streak_best": new_best}
