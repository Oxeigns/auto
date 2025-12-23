import json
import time
import signal
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Set, Optional

from pyrogram import Client
from pyrogram.types import ChatJoinRequest
from pyrogram.errors import FloodWait, RPCError


# ----------------------------
# Config + helpers
# ----------------------------

def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

@dataclass
class ApproveConfig:
    dry_run: bool
    per_approve_delay_sec: float
    max_approvals_per_minute: int
    rescan_every_minutes: int
    rescan_limit_per_chat: int

class MinuteLimiter:
    """
    Simple rolling limiter: allow max N actions per 60 seconds.
    """
    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._times = []  # timestamps

    async def acquire(self):
        if self.max_per_minute <= 0:
            return
        now = time.time()
        # keep only last 60s
        self._times = [t for t in self._times if now - t < 60]
        if len(self._times) >= self.max_per_minute:
            oldest = self._times[0]
            sleep_for = max(0.0, 60 - (now - oldest)) + 0.05
            await asyncio.sleep(sleep_for)
        self._times.append(time.time())

@dataclass
class Stats:
    old_approved: int = 0
    new_approved: int = 0
    errors: int = 0
    floodwaits: int = 0
    skipped_no_permission: int = 0


# ----------------------------
# Main app
# ----------------------------

cfg = load_config()
setup_logging(cfg.get("LOGGING", {}).get("LEVEL", "INFO"))

API_ID = int(cfg["API_ID"])
API_HASH = cfg["API_HASH"]
SESSION_STRING = cfg["SESSION_STRING"].strip()

if not SESSION_STRING:
    raise SystemExit("❌ SESSION_STRING missing in config.json")

TARGET_CHATS: Set[int] = set(cfg.get("TARGET_CHAT_IDS", []))
if not TARGET_CHATS:
    raise SystemExit("❌ TARGET_CHAT_IDS is empty in config.json")

a = cfg.get("APPROVE", {})
approve_cfg = ApproveConfig(
    dry_run=bool(a.get("DRY_RUN", False)),
    per_approve_delay_sec=float(a.get("PER_APPROVE_DELAY_SEC", 0.7)),
    max_approvals_per_minute=int(a.get("MAX_APPROVALS_PER_MINUTE", 50)),
    rescan_every_minutes=int(a.get("RESCAN_EVERY_MINUTES", 10)),
    rescan_limit_per_chat=int(a.get("RESCAN_LIMIT_PER_CHAT", 1000)),
)

app = Client(
    name="user_session_auto_approve",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

stats = Stats()
limiter = MinuteLimiter(approve_cfg.max_approvals_per_minute)
stop_event = asyncio.Event()

# Cache permission status per chat to avoid spamming API
permission_cache: Dict[int, bool] = {}


async def has_admin_invite_permission(chat_id: int) -> bool:
    """
    Returns True if the user session is admin/owner in the chat and can approve join requests
    (practically: can_invite_users is usually required).
    """
    if chat_id in permission_cache:
        return permission_cache[chat_id]

    try:
        me = await app.get_me()
        member = await app.get_chat_member(chat_id, me.id)

        status = getattr(member, "status", None)
        if status not in ("administrator", "owner"):
            permission_cache[chat_id] = False
            return False

        # For admins, check invite permission if available
        priv = getattr(member, "privileges", None)
        if priv is None:
            # Owner / or older structure - assume OK
            permission_cache[chat_id] = True
            return True

        can_invite = bool(getattr(priv, "can_invite_users", False))
        # Some chats may still allow approving if admin; but safest is require invite permission
        permission_cache[chat_id] = can_invite
        return can_invite

    except Exception as e:
        logging.warning(f"[perm-check] chat={chat_id} error={e}")
        permission_cache[chat_id] = False
        return False


async def safe_approve(req: ChatJoinRequest, is_old: bool) -> None:
    """
    Approve one request with safety:
    - per-minute limiter
    - per-approve delay
    - FloodWait handling
    """
    await limiter.acquire()

    if approve_cfg.dry_run:
        logging.info(f"[DRY_RUN] Would approve user={req.from_user.id} chat={req.chat.id}")
        await asyncio.sleep(approve_cfg.per_approve_delay_sec)
        return

    try:
        await req.approve()
        if is_old:
            stats.old_approved += 1
            logging.info(f"[OLD] approved user={req.from_user.id} chat={req.chat.id}")
        else:
            stats.new_approved += 1
            logging.info(f"[NEW] approved user={req.from_user.id} chat={req.chat.id}")

        await asyncio.sleep(approve_cfg.per_approve_delay_sec)

    except FloodWait as fw:
        stats.floodwaits += 1
        wait_s = int(getattr(fw, "value", 0)) or 5
        logging.warning(f"[FloodWait] sleeping {wait_s}s")
        await asyncio.sleep(wait_s)

    except RPCError as e:
        stats.errors += 1
        logging.error(f"[RPCError] approve failed: {e}")

    except Exception as e:
        stats.errors += 1
        logging.error(f"[Error] approve failed: {e}")


async def approve_backlog_for_chat(chat_id: int) -> None:
    """
    Approve pending (old) join requests for a single chat.
    """
    ok = await has_admin_invite_permission(chat_id)
    if not ok:
        stats.skipped_no_permission += 1
        logging.warning(f"[SKIP] No admin/invite permission for chat={chat_id}")
        return

    approved_here = 0
    try:
        async for req in app.get_chat_join_requests(chat_id, limit=approve_cfg.rescan_limit_per_chat):
            await safe_approve(req, is_old=True)
            approved_here += 1

        logging.info(f"[BACKLOG DONE] chat={chat_id} approved={approved_here}")

    except FloodWait as fw:
        stats.floodwaits += 1
        wait_s = int(getattr(fw, "value", 0)) or 5
        logging.warning(f"[FloodWait backlog] chat={chat_id} sleeping {wait_s}s")
        await asyncio.sleep(wait_s)

    except Exception as e:
        stats.errors += 1
        logging.error(f"[BACKLOG ERROR] chat={chat_id} error={e}")


async def approve_backlog_all_chats() -> None:
    logging.info("🔄 Backlog scan starting...")
    for chat_id in TARGET_CHATS:
        await approve_backlog_for_chat(chat_id)
    logging.info("✅ Backlog scan finished.")


@app.on_chat_join_request()
async def on_join_request(client: Client, join_request: ChatJoinRequest):
    chat_id = join_request.chat.id
    if chat_id not in TARGET_CHATS:
        return

    ok = await has_admin_invite_permission(chat_id)
    if not ok:
        stats.skipped_no_permission += 1
        logging.warning(f"[SKIP NEW] No admin/invite permission chat={chat_id}")
        return

    await safe_approve(join_request, is_old=False)


async def periodic_rescan_loop():
    """
    Periodically rescans backlog (professional safety net).
    """
    if approve_cfg.rescan_every_minutes <= 0:
        return

    while not stop_event.is_set():
        await asyncio.sleep(approve_cfg.rescan_every_minutes * 60)
        # In case permissions changed, refresh cache every cycle
        permission_cache.clear()
        await approve_backlog_all_chats()
        logging.info(
            f"[STATS] old={stats.old_approved} new={stats.new_approved} "
            f"errors={stats.errors} floodwaits={stats.floodwaits} "
            f"skipped_no_perm={stats.skipped_no_permission}"
        )


def handle_shutdown(*_):
    logging.warning("🛑 Shutdown requested...")
    stop_event.set()


async def main():
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    async with app:
        logging.info("✅ User session started.")
        # Initial backlog clear
        await approve_backlog_all_chats()

        # Start periodic rescan
        rescan_task = asyncio.create_task(periodic_rescan_loop())

        logging.info("🚀 Auto-approve active (old + new).")
        await stop_event.wait()

        rescan_task.cancel()
        logging.info(
            f"👋 Exiting. Final stats: old={stats.old_approved} new={stats.new_approved} "
            f"errors={stats.errors} floodwaits={stats.floodwaits} skipped_no_perm={stats.skipped_no_permission}"
        )

if __name__ == "__main__":
    asyncio.run(main())
