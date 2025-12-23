import os
import asyncio
import time
import signal
import traceback

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import ChatJoinRequest
from pyrogram.errors import FloodWait, UserAlreadyParticipant

# ================== CONFIG ==================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

INVITE_LINK = "https://t.me/+e8h5_XQmY5szYjUx"

PER_APPROVE_DELAY_SEC = float(os.getenv("PER_APPROVE_DELAY_SEC", "0.7"))
MAX_APPROVALS_PER_MINUTE = int(os.getenv("MAX_APPROVALS_PER_MINUTE", "50"))
RESCAN_EVERY_MINUTES = int(os.getenv("RESCAN_EVERY_MINUTES", "10"))

# ============================================

rate_bucket = []

def log(msg: str):
    print(msg, flush=True)

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

    status = member.status
    priv = getattr(member, "privileges", None)
    can_invite = getattr(priv, "can_invite_users", None)
    can_manage = getattr(priv, "can_manage_chat", None)

    log(f"[PERM] me={me.id} status={status} can_invite={can_invite} can_manage={can_manage}")

    # Owner ok
    if status == ChatMemberStatus.OWNER:
        return True

    # Admin required
    if status != ChatMemberStatus.ADMINISTRATOR:
        return False

    # Many channels work with invite/manage; if privileges missing, still try.
    return True

async def approve_user(client: Client, chat_id: int, user_id: int, tag: str) -> bool:
    await rate_limit()
    try:
        await client.approve_chat_join_request(chat_id, user_id)
        log(f"[ACCEPTED] {tag} user_id={user_id}")
        await asyncio.sleep(PER_APPROVE_DELAY_SEC)
        return True
    except FloodWait as e:
        log(f"[FLOODWAIT] {e.value}s")
        await asyncio.sleep(e.value)
        return False
    except Exception as ex:
        log(f"[ACCEPT_ERR] {tag} user_id={user_id} ex={ex}")
        log(traceback.format_exc())
        return False

async def drain_pending_requests(client: Client, chat_id: int):
    """
    Drain until pending becomes 0.
    Prints:
      - accepted_total
      - pending_seen_this_round
    """
    accepted_total = 0

    while True:
        pending = 0
        accepted_round = 0

        async for req in client.get_chat_join_requests(chat_id):
            pending += 1
            uid = getattr(req, "user_id", None) or getattr(getattr(req, "from_user", None), "id", None)
            if not uid:
                log("[WARN] request item has no user_id")
                continue

            if await approve_user(client, chat_id, uid, tag="OLD"):
                accepted_round += 1

        accepted_total += accepted_round
        log(f"[DRAIN_ROUND] accepted_round={accepted_round} pending_seen={pending}")

        # If no pending found, we're done draining
        if pending == 0:
            break

        # Safety: small pause then rescan
        await asyncio.sleep(2)

    log(f"[DRAIN_DONE] accepted_total={accepted_total}")
    return accepted_total

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
        session_string=SESSION_STRING,
    )

    async with app:
        log("Userbot started ✅")

        # Ensure user joined
        try:
            await app.join_chat(INVITE_LINK)
            log("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            log("Already joined channel ✅")

        # Resolve chat id
        chat = await app.get_chat(INVITE_LINK)
        CHAT_ID = chat.id
        log(f"Resolved chat ID: {CHAT_ID}")

        # Permission check
        ok = await check_and_log_permission(app, CHAT_ID)
        if not ok:
            log(f"[NO_PERMISSION] chat_id={CHAT_ID} (session user is not admin/owner)")
            return

        # New join requests (instant accept)
        @app.on_chat_join_request()
        async def handler(client: Client, req: ChatJoinRequest):
            try:
                if req.chat.id != CHAT_ID:
                    return
                await approve_user(client, CHAT_ID, req.from_user.id, tag="NEW")
            except Exception as ex:
                log(f"[HANDLER_ERR] {ex}")
                log(traceback.format_exc())

        # Drain old pending requests completely
        await drain_pending_requests(app, CHAT_ID)

        # Periodic rescans (in case Telegram missed events)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RESCAN_EVERY_MINUTES * 60)
            except asyncio.TimeoutError:
                await drain_pending_requests(app, CHAT_ID)

        log("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
