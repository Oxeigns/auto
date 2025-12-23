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

INVITE_LINK = os.getenv("INVITE_LINK", "https://t.me/+e8h5_XQmY5szYjUx")

# Speed controls (tune via env vars)
MAX_APPROVALS_PER_MINUTE = int(os.getenv("MAX_APPROVALS_PER_MINUTE", "80"))   # was 50
CONCURRENCY = int(os.getenv("CONCURRENCY", "4"))                              # 2-6 recommended
RESCAN_EVERY_MINUTES = int(os.getenv("RESCAN_EVERY_MINUTES", "10"))

# Extra backoff (auto increases on FloodWait)
BASE_BACKOFF_SEC = float(os.getenv("BASE_BACKOFF_SEC", "0.0"))

# ============================================

def log(msg: str):
    print(msg, flush=True)

# Token-bucket-ish limiter: ensures avg MAX_PER_MIN rate
_min_interval = 60.0 / max(1, MAX_APPROVALS_PER_MINUTE)
_last_sent = 0.0
_rate_lock = asyncio.Lock()

# Dynamic backoff that grows when FloodWait happens
_backoff = BASE_BACKOFF_SEC
_backoff_lock = asyncio.Lock()

async def rate_limit():
    global _last_sent
    async with _rate_lock:
        now = time.time()
        wait = (_last_sent + _min_interval) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_sent = time.time()

async def get_backoff():
    async with _backoff_lock:
        return _backoff

async def bump_backoff(seconds: float):
    """Increase backoff up to a cap, then slowly decay later."""
    global _backoff
    async with _backoff_lock:
        _backoff = min(10.0, max(_backoff, seconds))  # cap at 10s

async def decay_backoff():
    """Slowly decrease backoff if everything is smooth."""
    global _backoff
    async with _backoff_lock:
        _backoff = max(BASE_BACKOFF_SEC, _backoff * 0.85)

async def check_and_log_permission(client: Client, chat_id: int) -> bool:
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    status = member.status
    priv = getattr(member, "privileges", None)
    can_invite = getattr(priv, "can_invite_users", None)
    can_manage = getattr(priv, "can_manage_chat", None)

    log(f"[PERM] me={me.id} status={status} can_invite={can_invite} can_manage={can_manage}")

    if status == ChatMemberStatus.OWNER:
        return True
    if status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return True  # Telegram will enforce final permission on action

def extract_user_id(item):
    u = getattr(item, "user", None)
    if u is not None:
        uid = getattr(u, "id", None)
        if uid:
            return uid

    fu = getattr(item, "from_user", None)
    if fu is not None:
        uid = getattr(fu, "id", None)
        if uid:
            return uid

    uid = getattr(item, "user_id", None)
    if uid:
        return uid

    return None

async def approve_user(client: Client, chat_id: int, user_id: int, tag: str) -> bool:
    # Global rate limit + dynamic backoff
    await rate_limit()
    b = await get_backoff()
    if b > 0:
        await asyncio.sleep(b)

    try:
        await client.approve_chat_join_request(chat_id, user_id)
        log(f"[ACCEPTED] {tag} user_id={user_id}")
        # Smooth run -> slowly decay backoff
        await decay_backoff()
        return True

    except FloodWait as e:
        log(f"[FLOODWAIT] {e.value}s (auto backoff)")
        await bump_backoff(min(10.0, float(e.value)))
        await asyncio.sleep(e.value)
        return False

    except Exception as ex:
        log(f"[ACCEPT_ERR] {tag} user_id={user_id} ex={ex}")
        log(traceback.format_exc())
        return False

async def drain_pending_requests_fast(client: Client, chat_id: int):
    """
    Faster drain:
      1) collect all pending user_ids
      2) approve using worker queue with limited concurrency
    """
    # Collect pending IDs (fast)
    pending_ids = []
    async for item in client.get_chat_join_requests(chat_id):
        uid = extract_user_id(item)
        if uid:
            pending_ids.append(uid)

    pending = len(pending_ids)
    if pending == 0:
        log("[DRAIN] pending_seen=0")
        return 0, 0

    # Queue + workers
    q = asyncio.Queue()
    for uid in pending_ids:
        q.put_nowait(uid)

    accepted = 0
    accepted_lock = asyncio.Lock()

    async def worker(worker_id: int):
        nonlocal accepted
        while True:
            try:
                uid = await q.get()
            except asyncio.CancelledError:
                return

            try:
                ok = await approve_user(client, chat_id, uid, tag="OLD")
                if ok:
                    async with accepted_lock:
                        accepted += 1
            finally:
                q.task_done()

    workers = [asyncio.create_task(worker(i)) for i in range(CONCURRENCY)]
    await q.join()

    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    log(f"[DRAIN_SUMMARY] accepted={accepted} pending_seen={pending}")
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
        session_string=SESSION_STRING,
    )

    async with app:
        log("Userbot started ✅")

        # Ensure joined
        try:
            await app.join_chat(INVITE_LINK)
            log("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            log("Already joined channel ✅")

        # Resolve chat id
        chat = await app.get_chat(INVITE_LINK)
        CHAT_ID = chat.id
        log(f"Resolved chat ID: {CHAT_ID}")

        ok = await check_and_log_permission(app, CHAT_ID)
        if not ok:
            log(f"[NO_PERMISSION] chat_id={CHAT_ID}")
            return

        # New join requests: approve immediately (still rate-limited)
        @app.on_chat_join_request()
        async def handler(client: Client, req: ChatJoinRequest):
            try:
                if req.chat.id != CHAT_ID:
                    return
                await approve_user(client, CHAT_ID, req.from_user.id, tag="NEW")
            except Exception as ex:
                log(f"[HANDLER_ERR] {ex}")
                log(traceback.format_exc())

        # Drain old pending requests (fast)
        await drain_pending_requests_fast(app, CHAT_ID)

        # Periodic rescans
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RESCAN_EVERY_MINUTES * 60)
            except asyncio.TimeoutError:
                await drain_pending_requests_fast(app, CHAT_ID)

        log("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
