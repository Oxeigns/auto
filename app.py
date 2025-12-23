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

def _priv_bool(member, attr: str):
    p = getattr(member, "privileges", None)
    if not p:
        return None
    return getattr(p, attr, None)

async def check_and_log_permission(client: Client, chat_id: int) -> bool:
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    status = getattr(member, "status", None)
    can_invite = _priv_bool(member, "can_invite_users")
    can_manage = _priv_bool(member, "can_manage_chat")
    can_promote = _priv_bool(member, "can_promote_members")
    can_restrict = _priv_bool(member, "can_restrict_members")

    print(
        f"[PERM] me={me.id} status={status} "
        f"can_invite={can_invite} can_manage={can_manage} "
        f"can_restrict={can_restrict} can_promote={can_promote}"
    )

    # Owner always ok
    if status == "owner":
        return True

    # Admin required
    if status != "administrator":
        return False

    # Approving join-requests depends on chat type/admin rights.
    # Most cases: can_invite_users or can_manage_chat.
    if can_invite is True or can_manage is True:
        return True

    # If privileges missing/None, treat as not ok
    return False

async def approve_user(client: Client, chat_id: int, user_id: int, tag: str):
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
        print(f"[APPROVE_ERR] user_id={user_id} ex={ex}")
        return False

async def backlog_scan(client: Client, chat_id: int):
    """
    Prints:
    - pending count (seen in scan)
    - accepted count (approved)
    """
    accepted = 0
    pending = 0

    try:
        ok = await check_and_log_permission(client, chat_id)
        if not ok:
            print(f"[NO_PERMISSION] chat_id={chat_id}")
            return accepted, pending

        async for j in client.get_chat_join_requests(chat_id):
            pending += 1
            uid = getattr(j, "user_id", None) or getattr(getattr(j, "from_user", None), "id", None)
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

        # Join via invite link
        try:
            await app.join_chat(INVITE_LINK)
            print("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            print("Already joined channel ✅")

        # Resolve chat
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

                uid = req.from_user.id
                await approve_user(client, CHAT_ID, uid, tag="NEW")

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as ex:
                print("Handler error:", ex)

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
