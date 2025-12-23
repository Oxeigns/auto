import os
import asyncio
import time
import signal

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import ChatJoinRequest
from pyrogram.errors import FloodWait, PeerIdInvalid, UserAlreadyParticipant


# ================== CONFIG ==================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

# ✅ Your channel invite link (hard-coded)
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

async def check_and_log_permission(client: Client, chat_id: int) -> bool:
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    status = member.status  # enum
    priv = getattr(member, "privileges", None)

    can_invite = getattr(priv, "can_invite_users", None)
    can_manage = getattr(priv, "can_manage_chat", None)
    can_restrict = getattr(priv, "can_restrict_members", None)
    can_promote = getattr(priv, "can_promote_members", None)

    print(
        f"[PERM] me={me.id} status={status} "
        f"can_invite={can_invite} can_manage={can_manage} "
        f"can_restrict={can_restrict} can_promote={can_promote}"
    )

    if status == ChatMemberStatus.OWNER:
        return True

    if status != ChatMemberStatus.ADMINISTRATOR:
        return False

    # For channel join-requests, invite/manage is typically sufficient
    return bool(can_invite or can_manage)

async def approve_user(client: Client, chat_id: int, user_id: int, tag: str) -> bool:
    await rate_limit()
    try:
        await client.approve_chat_join_request(chat_id, user_id)
        print(f"[APPROVED] {tag} user_id={user_id}")
        await asyncio.sleep(PER_APPROVE_DELAY_SEC)
        return True
    except FloodWait as e:
        print(f"[FLOODWAIT] {e.value}s")
        await asyncio.sleep(e.value)
        return False
    except Exception as ex:
        print(f"[APPROVE_ERR] tag={tag} user_id={user_id} ex={ex}")
        return False

async def backlog_scan(client: Client, chat_id: int):
    accepted = 0
    pending = 0

    try:
        ok = await check_and_log_permission(client, chat_id)
        if not ok:
            print(f"[NO_PERMISSION] chat_id={chat_id}")
            return accepted, pending

        # Warm up peer
        await client.get_chat(chat_id)

        async for item in client.get_chat_join_requests(chat_id):
            pending += 1
            uid = getattr(item, "user_id", None) or getattr(getattr(item, "from_user", None), "id", None)
            if not uid:
                print("[WARN] pending item without user_id")
                continue

            if await approve_user(client, chat_id, uid, tag="OLD"):
                accepted += 1

    except PeerIdInvalid:
        print("[PEER_INVALID] chat not resolved/joined")
    except FloodWait as e:
        print(f"[FLOODWAIT] {e.value}s")
        await asyncio.sleep(e.value)
    except Exception as ex:
        print(f"[SCAN_ERR] ex={ex}")

    print(f"[SCAN_SUMMARY] chat_id={chat_id} accepted={accepted} pending_seen={pending}")
    return accepted, pending

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

        # Join channel via invite link (userbot account)
        try:
            await app.join_chat(INVITE_LINK)
            print("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            print("Already joined channel ✅")

        # Resolve chat id
        chat = await app.get_chat(INVITE_LINK)
        CHAT_ID = chat.id
        print("Resolved chat ID:", CHAT_ID)

        # New join requests
        @app.on_chat_join_request()
        async def handler(client: Client, req: ChatJoinRequest):
            try:
                if req.chat.id != CHAT_ID:
                    return

                ok = await check_and_log_permission(client, CHAT_ID)
                if not ok:
                    print(f"[NO_PERMISSION] new_request chat_id={CHAT_ID}")
                    return

                await approve_user(client, CHAT_ID, req.from_user.id, tag="NEW")
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as ex:
                print("[HANDLER_ERR]", ex)

        # Initial scan
        await backlog_scan(app, CHAT_ID)

        # Periodic scan
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RESCAN_EVERY_MINUTES * 60)
            except asyncio.TimeoutError:
                await backlog_scan(app, CHAT_ID)

        print("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
