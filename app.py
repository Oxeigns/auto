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

# 🔥 DIRECT INVITE LINK HERE
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
        await asyncio.sleep(60 - (now - rate_bucket[0]))
    rate_bucket.append(time.time())

async def has_permission(client: Client, chat_id: int) -> bool:
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    if member.status not in ("administrator", "owner"):
        return False

    if member.privileges:
        return member.privileges.can_invite_users

    return True

async def approve(req: ChatJoinRequest, old=False):
    await rate_limit()
    try:
        await req.approve()
        print(("OLD" if old else "NEW"), "approved:", req.from_user.id)
        await asyncio.sleep(PER_APPROVE_DELAY_SEC)
    except FloodWait as e:
        await asyncio.sleep(e.value)

async def main():
    stop_event = asyncio.Event()

    def shutdown(*_):
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    app = Client(
        "user",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING
    )

    async with app:
        print("Userbot started ✅")

        # ✅ JOIN VIA INVITE LINK
        try:
            await app.join_chat(INVITE_LINK)
            print("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            print("Already joined channel ✅")

        # ✅ RESOLVE CHAT (NO PEER ERROR)
        chat = await app.get_chat(INVITE_LINK)
        CHAT_ID = chat.id
        print("Resolved chat ID:", CHAT_ID)

        @app.on_chat_join_request()
        async def handler(client: Client, req: ChatJoinRequest):
            if req.chat.id == CHAT_ID:
                if await has_permission(client, CHAT_ID):
                    await approve(req)

        # 🔁 BACKLOG SCAN
        async def clear_old():
            try:
                async for req in app.get_chat_join_requests(CHAT_ID):
                    await approve(req, old=True)
            except PeerIdInvalid:
                print("Peer not resolved – userbot not joined")
            except FloodWait as e:
                await asyncio.sleep(e.value)

        await clear_old()

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=RESCAN_EVERY_MINUTES * 60
                )
            except asyncio.TimeoutError:
                await clear_old()

        print("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
