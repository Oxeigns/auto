## Auto Approve Pro

Auto approve Telegram **channel join requests** using **Pyrogram USER session (session string)**.

### ✅ Features
- Approves **old pending** join requests (backlog drain)
- Approves **new** join requests instantly
- Auto **rate-limit + backoff** to avoid FloodWait
- Auto **speed tuning** (APM increases/decreases automatically)
- **Concurrency** auto-detected (no env var needed)
- Skips spammy users & prevents repeat errors:
  - `USER_CHANNELS_TOO_MUCH` → auto skip + block for 24h
  - duplicate user approvals → auto skip
- Live **stats logs** in Heroku logs

---

### Deploy to Heroku
[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/Oxeigns/auto)

---

### ✅ Required Config Vars (Heroku)
Go to **Settings → Config Vars** and add:

| Key | Value |
|---|---|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API HASH |
| `SESSION_STRING` | Pyrogram session string |

> ⚠️ Use this Telegram account only on Heroku (avoid `AUTH_KEY_DUPLICATED`).

---

### ⚙️ Optional Config Vars
| Key | Default | What it does |
|---|---:|---|
| `RESCAN_EVERY_MINUTES` | `1` | Backup rescan interval |
| `START_APPROVALS_PER_MIN` | `80` | Starting speed |
| `MIN_APPROVALS_PER_MIN` | `30` | Minimum speed |
| `MAX_APPROVALS_PER_MIN_CAP` | `180` | Maximum speed cap |
| `STATS_INTERVAL_SEC` | `60` | How often stats print |
| `BLOCK_TTL_SEC` | `86400` | Block time for bad users |
| `RECENT_TTL_SEC` | `3600` | Duplicate-skip memory window |

---

### ✅ Telegram Setup
1. Enable **Join Requests** on your channel
2. Add your **USER account** (session string) as **Admin**
3. Keep this user logged in only on Heroku

---

### 📌 Logs You’ll See
- Approvals:
  - `[ACCEPTED] OLD user_id=...`
  - `[ACCEPTED] NEW user_id=...`
- Skips:
  - `[SKIP_TOO_MANY_CHANNELS] user_id=...`
  - `[SKIP_DUP] user_id=...`
- Stats:
  - `[STATS] accepted=... pending_last=... apm=.../min backoff=...s conc=...`

---
