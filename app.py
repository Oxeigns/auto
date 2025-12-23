import os
import asyncio
import time
import signal
from pyrogram import Client
from pyrogram.types import ChatJoinRequest
from pyrogram.errors import FloodWait, PeerIdInvalid, UserAlreadyParticipant

# ================== CONFIG ==================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

INVITE_LINK = "https://t.me/+e8h5_XQmY5szYjUx"

PER_APPROVE_DELAY_SEC = 0.7
MAX_APPROVALS_PER_MINUTE = 50
RESCAN_EVERY_MINUTES = 10

# ============================================

rate_bucket = []

async def rate_limit():
    now = time.time()
    global rate_bucket
    rate_bucket = [t for t in rate_bucket if now - t < 60]
    if len(rate_bucket) >= MAX_APPROVALS_PER_MINUTE:
        sleep_for = 60 - (now - rate_bucket[0])
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
    rate_bucket.append(time.time())

async def has_permission(client: Client, chat_id: int) -> bool:
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    if member.status not in ("administrator", "owner"):
        return False
    if member.privileges:
        return bool(member.privileges.can_invite_users)
    return True

async def approve_user(client: Client, chat_id: int, user_id: int, old: bool = False):
    await rate_limit()
    try:
        # ✅ Correct method (works for both new events + backlog items)
        await client.approve_chat_join_request(chat_id, user_id)
        print(("OLD" if old else "NEW"), "approved:", user_id)
        await asyncio.sleep(PER_APPROVE_DELAY_SEC)
    except FloodWait as e:
        await asyncio.sleep(e.value)

async def main():
    stop_event = asyncio.Event()

    def shutdown(*_):
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    except Exception:
        pass

    app = Client(
        "user",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING
    )

    async with app:
        print("Userbot started ✅")

        # ✅ Join via invite link (userbot account)
        try:
            await app.join_chat(INVITE_LINK)
            print("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            print("Already joined channel ✅")

        # ✅ Resolve chat
        chat = await app.get_chat(INVITE_LINK)
        CHAT_ID = chat.id
        print("Resolved chat ID:", CHAT_ID)

        # ✅ New join requests
        @app.on_chat_join_request()
        async def handler(client: Client, req: ChatJoinRequest):
            try:
                if req.chat.id == CHAT_ID and await has_permission(client, CHAT_ID):
                    await approve_user(client, CHAT_ID, req.from_user.id, old=False)
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as ex:
                print("Handler error:", ex)

        # ✅ Backlog scan
        async def clear_old():
            try:
                if not await has_permission(app, CHAT_ID):
                    print("No permission in", CHAT_ID)
                    return

                async for j in app.get_chat_join_requests(CHAT_ID):
                    # j may be ChatJoiner-like → use user_id safely
                    uid = getattr(j, "user_id", None) or getattr(getattr(j, "from_user", None), "id", None)
                    if uid:
                        await approve_user(app, CHAT_ID, uid, old=True)

            except PeerIdInvalid:
                print("Peer not resolved – userbot not joined")
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as ex:
                print("Clear old error:", ex)

        await clear_old()

        # ✅ Periodic rescan
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RESCAN_EVERY_MINUTES * 60)
            except asyncio.TimeoutError:
                await clear_old()

        print("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
