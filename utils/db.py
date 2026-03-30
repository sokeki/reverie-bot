from motor.motor_asyncio import AsyncIOMotorCollection


def _default_doc(user_id: int, guild_id: int) -> dict:
    return {
        "user_id": user_id,
        "guild_id": guild_id,
        "points": 0,
        "voice_minutes": 0,
        "messages_sent": 0,
    }


async def get_user(
    users_col: AsyncIOMotorCollection, user_id: int, guild_id: int
) -> dict:
    """Fetch a user doc, always ensuring all fields exist."""
    # Use findOneAndUpdate with $setOnInsert so a newly upserted doc
    # always has every field, and an existing doc is never overwritten.
    doc = await users_col.find_one_and_update(
        {"user_id": user_id, "guild_id": guild_id},
        {"$setOnInsert": _default_doc(user_id, guild_id)},
        upsert=True,
        return_document=True,
    )
    # Backfill any missing fields on older docs
    for field, default in [("points", 0), ("voice_minutes", 0), ("messages_sent", 0)]:
        if field not in doc:
            doc[field] = default
    return doc


async def add_points(
    users_col: AsyncIOMotorCollection, user_id: int, guild_id: int, amount: int
):
    """Add (or subtract) points for a user, ensuring the doc exists first."""
    # Ensure the document exists with all fields before incrementing,
    # so $inc and $setOnInsert never touch the same field in one operation.
    await users_col.update_one(
        {"user_id": user_id, "guild_id": guild_id},
        {"$setOnInsert": _default_doc(user_id, guild_id)},
        upsert=True,
    )
    await users_col.update_one(
        {"user_id": user_id, "guild_id": guild_id},
        {"$inc": {"points": amount}},
    )
