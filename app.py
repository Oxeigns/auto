import os
import json
import asyncio
import time
import signal
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
            "TARGET_CHAT_IDS": [int(x.strip()) for x in os.getenv("TARGET_CHAT_IDS").split(",") if x.strip()],
            "PER_APPROVE_DELAY_SEC": float(os.getenv("PER_APPROVE_DELAY_SEC", "0.7")),
            "MAX_APPROVALS_PER_MINUTE": int(os.getenv("MAX_APPROVALS_PER_MINUTE", "50")),
            "RESCAN_EVERY_MINUTES": int(os.getenv("RESCAN_EVERY_MINUTES", "10")),
        }

    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

cfg = load_config()

TARGET_CHATS = set(cfg["TARGET_CHAT_IDS"])
DELAY = float(cfg["PER_APPROVE_DELAY_SEC"])
MAX_PER_MIN = int(cfg["MAX_APPROVALS_PER_MINUTE"])
RESCAN_MIN = int(cfg["RESCAN_EVERY_MINUTES"])

# ---------------- RATE LIMIT BUCKET ----------------

rate_bucket = []

async def rate_limit():
    """Simple sliding window rate limiter: MAX_PER_MIN approvals per 60 seconds."""
    now = time.time()
    global rate_bucket
    rate_bucket = [t for t in rate_bucket if now - t < 60]

    if len(rate_bucket) >= MAX_PER_MIN:
        sleep_for = 60 - (now - rate_bucket[0])
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    rate_bucket.append(time.time())

# ---------------- HELPERS ----------------

async def has_permission(client: Client, chat_id: int) -> bool:
    """Checks if user is admin/owner and can invite/approve users."""
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    if member.status not in ("administrator", "owner"):
        return False

    # Some chats may not expose privileges fully; default True for admin/owner.
    if member.privileges is not None:
        # can_invite_users is generally enough for join request approvals in many cases.
        return bool(member.privileges.can_invite_users)

    return True

async def approve(req: ChatJoinRequest, old: bool = False):
    await rate_limit()
    try:
        await req.approve()
        print(("OLD" if old else "NEW"), "approved:", req.from_user.id)
        await asyncio.sleep(DELAY)
    except FloodWait as e:
        await asyncio.sleep(e.value)

async def clear_old(client: Client):
    """Rescan backlog join requests and approve them."""
    for chat_id in TARGET_CHATS:
        try:
            if not await has_permission(client, chat_id):
                print("No permission in", chat_id)
                continue

            async for req in client.get_chat_join_requests(chat_id):
                await approve(req, old=True)

        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as ex:
            print(f"Error while scanning chat {chat_id}: {ex}")

# ---------------- MAIN ----------------

async def main():
    stop_event = asyncio.Event()

    def _graceful_stop(*_):
        stop_event.set()

    # Graceful shutdown signals (Heroku sends SIGTERM)
    try:
        signal.signal(signal.SIGTERM, _graceful_stop)
        signal.signal(signal.SIGINT, _graceful_stop)
    except Exception:
        pass

    app = Client(
        "user",
        api_id=int(cfg["API_ID"]),
        api_hash=str(cfg["API_HASH"]),
        session_string=str(cfg["SESSION_STRING"]),
    )

    @app.on_chat_join_request()
    async def handler(client: Client, req: ChatJoinRequest):
        try:
            if req.chat and req.chat.id in TARGET_CHATS:
                if await has_permission(client, req.chat.id):
                    await approve(req, old=False)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as ex:
            print("Handler error:", ex)

    async with app:
        print("Userbot started ✅")
        await clear_old(app)

        while not stop_event.is_set():
            try:
                # Wait RESCAN_MIN minutes OR until stop_event
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=RESCAN_MIN * 60)
                except asyncio.TimeoutError:
                    pass

                if stop_event.is_set():
                    break

                await clear_old(app)

            except Exception as ex:
                print("Main loop error:", ex)
                await asyncio.sleep(3)

        print("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
