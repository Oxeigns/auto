import os
import asyncio
import time
import signal
import traceback

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import ChatJoinRequest
from pyrogram.errors import FloodWait, UserAlreadyParticipant, UserChannelsTooMuch

# ================== CONFIG ==================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

INVITE_LINK = "https://t.me/+e8h5_XQmY5szYjUx"

# Auto defaults (no need Heroku vars, but you CAN override via env)
RESCAN_EVERY_MINUTES = int(os.getenv("RESCAN_EVERY_MINUTES", "1"))

MIN_APPROVALS_PER_MIN = int(os.getenv("MIN_APPROVALS_PER_MIN", "30"))
MAX_APPROVALS_PER_MIN_CAP = int(os.getenv("MAX_APPROVALS_PER_MIN_CAP", "180"))
START_APPROVALS_PER_MIN = int(os.getenv("START_APPROVALS_PER_MIN", "80"))

BASE_BACKOFF_SEC = float(os.getenv("BASE_BACKOFF_SEC", "0.0"))
STATS_INTERVAL_SEC = int(os.getenv("STATS_INTERVAL_SEC", "60"))
RECENT_TTL_SEC = int(os.getenv("RECENT_TTL_SEC", "3600"))  # 1 hour

# Block users that can never be approved (e.g., USER_CHANNELS_TOO_MUCH)
BLOCK_TTL_SEC = int(os.getenv("BLOCK_TTL_SEC", "86400"))  # 24 hours

TUNE_STEP_UP = int(os.getenv("TUNE_STEP_UP", "10"))
TUNE_STEP_DOWN = int(os.getenv("TUNE_STEP_DOWN", "20"))
SMOOTH_SUCCESS_TARGET = int(os.getenv("SMOOTH_SUCCESS_TARGET", "30"))
# ============================================


def log(msg: str):
    print(msg, flush=True)


# ---------------- AUTO CONCURRENCY (no vars needed) ----------------
def auto_concurrency() -> int:
    cpu = os.cpu_count() or 1
    if cpu <= 1:
        return 4  # safe default; rate-limit will protect
    if cpu == 2:
        return 4
    return min(6, max(2, cpu))

CONCURRENCY = int(os.getenv("CONCURRENCY", str(auto_concurrency())))


# ---------------- STATS ----------------
stats = {
    "accepted": 0,
    "pending_last": 0,
    "errors": 0,
    "floodwaits": 0,
    "skipped_dup": 0,
    "skipped_blocked": 0,
    "skipped_too_many_channels": 0,
    "accept_errs": 0,
    "smooth_success": 0,
}

# ---------------- DUPLICATE SKIP ----------------
recent_seen = {}  # user_id -> timestamp


def dup_check_and_mark(user_id: int) -> bool:
    now = time.time()
    for k, t in list(recent_seen.items()):
        if now - t > RECENT_TTL_SEC:
            del recent_seen[k]

    if user_id in recent_seen:
        stats["skipped_dup"] += 1
        return True

    recent_seen[user_id] = now
    return False


# ---------------- BLOCKLIST (to stop repeated spam errors) ----------------
blocked_until = {}  # user_id -> unblock_timestamp


def is_blocked(user_id: int) -> bool:
    now = time.time()
    u = blocked_until.get(user_id)
    if u and now < u:
        stats["skipped_blocked"] += 1
        return True
    if u and now >= u:
        blocked_until.pop(user_id, None)
    return False


# ---------------- RATE LIMIT + BACKOFF + AUTO APM ----------------
_rate_lock = asyncio.Lock()
_last_sent = 0.0

_backoff = BASE_BACKOFF_SEC
_backoff_lock = asyncio.Lock()

_apm = START_APPROVALS_PER_MIN
_apm_lock = asyncio.Lock()


async def get_apm() -> int:
    async with _apm_lock:
        return _apm


async def set_apm(new_val: int):
    global _apm
    async with _apm_lock:
        _apm = max(MIN_APPROVALS_PER_MIN, min(MAX_APPROVALS_PER_MIN_CAP, int(new_val)))


async def tune_down():
    cur = await get_apm()
    await set_apm(cur - TUNE_STEP_DOWN)


async def tune_up_if_smooth():
    if stats["smooth_success"] >= SMOOTH_SUCCESS_TARGET:
        cur = await get_apm()
        await set_apm(cur + TUNE_STEP_UP)
        stats["smooth_success"] = 0


async def rate_limit():
    global _last_sent
    async with _rate_lock:
        apm = await get_apm()
        min_interval = 60.0 / max(1, apm)
        now = time.time()
        wait = (_last_sent + min_interval) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_sent = time.time()


async def get_backoff() -> float:
    async with _backoff_lock:
        return _backoff


async def bump_backoff(seconds: float):
    global _backoff
    async with _backoff_lock:
        _backoff = min(10.0, max(_backoff, float(seconds)))


async def decay_backoff():
    global _backoff
    async with _backoff_lock:
        _backoff = max(BASE_BACKOFF_SEC, _backoff * 0.85)


# ---------------- PERMISSION ----------------
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
    return True


# ---------------- JOIN REQUEST HELPERS ----------------
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
    # Stop repeated spam errors
    if is_blocked(user_id):
        log(f"[SKIP_BLOCKED] user_id={user_id}")
        return False

    # Skip duplicates
    if dup_check_and_mark(user_id):
        log(f"[SKIP_DUP] user_id={user_id}")
        return False

    await rate_limit()
    b = await get_backoff()
    if b > 0:
        await asyncio.sleep(b)

    try:
        await client.approve_chat_join_request(chat_id, user_id)
        stats["accepted"] += 1
        stats["smooth_success"] += 1
        log(f"[ACCEPTED] {tag} user_id={user_id}")

        await decay_backoff()
        await tune_up_if_smooth()
        return True

    except UserChannelsTooMuch:
        # Telegram doesn't allow approval for this user (already too many channels)
        stats["skipped_too_many_channels"] += 1
        blocked_until[user_id] = time.time() + BLOCK_TTL_SEC
        log(f"[SKIP_TOO_MANY_CHANNELS] user_id={user_id} blocked_for={BLOCK_TTL_SEC}s")
        return False

    except FloodWait as e:
        stats["floodwaits"] += 1
        stats["smooth_success"] = 0
        log(f"[FLOODWAIT] {e.value}s (auto slow)")
        await bump_backoff(min(10.0, float(e.value)))
        await tune_down()
        await asyncio.sleep(e.value)
        return False

    except Exception as ex:
        stats["accept_errs"] += 1
        stats["smooth_success"] = 0
        log(f"[ACCEPT_ERR] {tag} user_id={user_id} ex={ex}")
        log(traceback.format_exc())
        await tune_down()
        return False


async def drain_pending_requests_fast(client: Client, chat_id: int):
    pending_ids = []
    async for item in client.get_chat_join_requests(chat_id):
        uid = extract_user_id(item)
        if uid:
            pending_ids.append(uid)

    pending = len(pending_ids)
    stats["pending_last"] = pending

    if pending == 0:
        log("[DRAIN] pending_seen=0")
        return 0, 0

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


# ---------------- MAIN ----------------
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

    async def stats_logger():
        while not stop_event.is_set():
            try:
                apm = await get_apm()
                b = await get_backoff()
                log(
                    "[STATS] "
                    f"accepted={stats['accepted']} pending_last={stats['pending_last']} "
                    f"errors={stats['errors']} accept_errs={stats['accept_errs']} "
                    f"floodwaits={stats['floodwaits']} skipped_dup={stats['skipped_dup']} "
                    f"skipped_blocked={stats['skipped_blocked']} "
                    f"skipped_too_many_channels={stats['skipped_too_many_channels']} "
                    f"apm={apm}/min backoff={b:.2f}s conc={CONCURRENCY}"
                )
            except Exception:
                pass
            await asyncio.sleep(STATS_INTERVAL_SEC)

    async with app:
        log("Userbot started ✅")
        log(
            f"[BOOT] conc={CONCURRENCY} start_apm={START_APPROVALS_PER_MIN}/min "
            f"range={MIN_APPROVALS_PER_MIN}-{MAX_APPROVALS_PER_MIN_CAP}/min "
            f"block_ttl={BLOCK_TTL_SEC}s"
        )

        try:
            await app.join_chat(INVITE_LINK)
            log("Joined channel via invite link ✅")
        except UserAlreadyParticipant:
            log("Already joined channel ✅")

        chat = await app.get_chat(INVITE_LINK)
        CHAT_ID = chat.id
        log(f"Resolved chat ID: {CHAT_ID}")

        ok = await check_and_log_permission(app, CHAT_ID)
        if not ok:
            log(f"[NO_PERMISSION] chat_id={CHAT_ID}")
            return

        stats_task = asyncio.create_task(stats_logger())

        @app.on_chat_join_request()
        async def handler(client: Client, req: ChatJoinRequest):
            try:
                if req.chat.id != CHAT_ID:
                    return
                await approve_user(client, CHAT_ID, req.from_user.id, tag="NEW")
            except Exception as ex:
                stats["errors"] += 1
                log(f"[HANDLER_ERR] {ex}")
                log(traceback.format_exc())

        # Initial drain
        await drain_pending_requests_fast(app, CHAT_ID)

        # Periodic rescans
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RESCAN_EVERY_MINUTES * 60)
            except asyncio.TimeoutError:
                try:
                    await drain_pending_requests_fast(app, CHAT_ID)
                except Exception as ex:
                    stats["errors"] += 1
                    log(f"[SCAN_ERR] {ex}")
                    log(traceback.format_exc())

        stats_task.cancel()
        log("Stopping userbot...")

if __name__ == "__main__":
    asyncio.run(main())
