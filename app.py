import os
import json
import asyncio
import time
from pyrogram import Client
from pyrogram.types import ChatJoinRequest
from pyrogram.errors import FloodWait

# ---------------- CONFIG LOADER ----------------

def load_config():
    if os.getenv("API_ID"):
        return {
            "API_ID": int(os.getenv("API_ID")),
            "API_HASH": os.getenv("API_HASH"),
            "SESSION_STRING": os.getenv("SESSION_STRING"),
            "TARGET_CHAT_IDS": [
                int(x.strip()) for x in os.getenv("TARGET_CHAT_IDS").split(",")
            ],
            "PER_APPROVE_DELAY_SEC": float(os.getenv("PER_APPROVE_DELAY_SEC", "0.7")),
            "MAX_APPROVALS_PER_MINUTE": int(os.getenv("MAX_APPROVALS_PER_MINUTE", "50")),
            "RESCAN_EVERY_MINUTES": int(os.getenv("RESCAN_EVERY_MINUTES", "10"))
        }

    with open("config.json", "r") as f:
        return json.load(f)

cfg = load_config()

# ---------------- APP INIT ----------------

app = Client(
    "user",
    api_id=cfg["API_ID"],
    api_hash=cfg["API_HASH"],
    session_string=cfg["SESSION_STRING"]
)

TARGET_CHATS = set(cfg["TARGET_CHAT_IDS"])
DELAY = cfg["PER_APPROVE_DELAY_SEC"]
MAX_PER_MIN = cfg["MAX_APPROVALS_PER_MINUTE"]
RESCAN_MIN = cfg["RESCAN_EVERY_MINUTES"]

rate_bucket = []

# ---------------- HELPERS ----------------

async def rate_limit():
    now = time.time()
    global rate_bucket
    rate_bucket = [t for t in rate_bucket if now - t < 60]
    if len(rate_bucket) >= MAX_PER_MIN:
        await asyncio.sleep(60 - (now - rate_bucket[0]))
    rate_bucket.append(time.time())

async def has_permission(chat_id):
    me = await app.get_me()
    member = await app.get_chat_member(chat_id, me.id)
    if member.status not in ("administrator", "owner"):
        return False
    if member.privileges:
        return member.privileges.can_invite_users
    return True

async def approve(req, old=False):
    await rate_limit()
    try:
        await req.approve()
        print(("OLD" if old else "NEW"), "approved:", req.from_user.id)
        await asyncio.sleep(DELAY)
    except FloodWait as e:
        await asyncio.sleep(e.value)

# ---------------- BACKLOG ----------------

async def clear_old():
    for chat in TARGET_CHATS:
        if not await has_permission(chat):
            print("No permission in", chat)
            continue
        async for req in app.get_chat_join_requests(chat):
            await approve(req, old=True)

# ---------------- NEW REQUESTS ----------------

@app.on_chat_join_request()
async def handler(_, req: ChatJoinRequest):
    if req.chat.id in TARGET_CHATS and await has_permission(req.chat.id):
        await approve(req)

# ---------------- MAIN ----------------

async def main():
    async with app:
        await clear_old()
        while True:
            await asyncio.sleep(RESCAN_MIN * 60)
            await clear_old()

asyncio.run(main())
