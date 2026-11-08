# WTR-Lab Crawler — Complete Guide

A reference for running the project, understanding how it works, and finding things in the code.

---

## 1. What this project is (and is not)

This is **not a website**. It is a **Telegram bot + background worker** that runs on your own Windows PC.

You send it a `wtr-lab.com` novel link in Telegram. It opens a real Chrome window, downloads the chapters, caches them on disk, builds an **EPUB**, and sends the EPUB back to you in Telegram.

| Piece | Technology |
|---|---|
| Chat interface | Telegram bot (`pyTelegramBotAPI` / `telebot`) |
| Fetching pages | Chrome driven by **SeleniumBase UC mode** (undetected Chrome, to get past Cloudflare) |
| HTML parsing | `beautifulsoup4` |
| Decrypting chapter text | `cryptography` (AES-GCM) |
| Building books | `ebooklib` |
| Queue + cache index | local **SQLite** file (`data/worker.sqlite3`) |
| Config | `.env` file via `python-dotenv` |

---

## 2. How it is "generated" — the pipeline

```
 You (Telegram)                 app.py process                           Chrome (SeleniumBase)
 ──────────────      ┌─────────────────────────────────┐
 /download  ───────► │ Thread 1: bot.infinity_polling  │
 paste URL           │   - checks ALLOWED_USER_IDS     │
 pick range          │   - checks daily limits         │
                     │   - INSERT row into tasks table │
                     │                                 │
                     │ Thread 2: worker_loop           │
                     │   - claim_task() (pending→running)
                     │   - process_task()  ───────────────────────────►  open novel page
                     │        load_novel()  reads __NEXT_DATA__ JSON ◄──  HTML
                     │        GET /api/chapters/<id>   (chapter list)
                     │        for each chapter not cached:
                     │           POST /api/reader/get  ───────────────►  encrypted body
                     │           decrypt_body() (AES-GCM) → text
                     │           apply glossary terms
                     │           write data/library/<id>/chapters/00001.xhtml
                     │           random sleep 10–18 s (throttle)
                     │        build_epub()  → data/library/<id>/artifacts/*.epub
                     │        send EPUB (+ cover) back to chat
 EPUB file  ◄─────── │                                 │
                     └─────────────────────────────────┘
```

Step by step:

1. **Queue.** `/download` → bot asks for a URL → you choose *All chapters* or *Custom range* (`40-60`). `insert_task()` ([app.py:645](app.py#L645)) writes a `pending` row to SQLite after checking `DAILY_TASK_LIMIT`.
2. **Claim.** The worker thread polls every 3 s; `claim_task()` ([app.py:704](app.py#L704)) flips one task to `running`.
3. **Load novel.** `WtrLabClient.load_novel()` ([app.py:2339](app.py#L2339)) opens the page in Chrome and parses the `<script id="__NEXT_DATA__">` JSON for title, author, synopsis, cover, novel ID. Then it calls `/api/chapters/<id>` for the chapter list.
4. **Fetch chapters.** For every chapter in the range that isn't already cached, it calls `/api/reader/get` from inside the browser (so cookies/Cloudflare clearance are reused). The body comes back encrypted (`arr:`/`str:` + `iv:tag:ciphertext`) and is decrypted by `decrypt_body()` ([app.py:1921](app.py#L1921)).
5. **Cache.** Each chapter becomes an XHTML file at `data/library/<novel_id>/chapters/00001.xhtml`, indexed in the `chapters` table. Re-running the same link resumes from cache — cached chapters are free and don't count toward limits.
6. **Build EPUB.** `build_epub()` ([app.py:3137](app.py#L3137)) reads cached chapters in order and writes the `.epub` into `data/library/<novel_id>/artifacts/`.
7. **Deliver.** `send_completed_files()` ([app.py:3536](app.py#L3536)) sends the cover + EPUB to your chat (and to `ADMIN_CHAT_ID` / `OUTPUT_GROUPS` if set).

### Cloudflare Turnstile
If Cloudflare shows a checkbox challenge, the worker pauses and tells you in Telegram. You tick the box in the visible Chrome window, then send `/solved`. Finished chapters stay cached, so nothing is lost.

---

## 3. Files in the folder

| File | Purpose |
|---|---|
| `app.py` (5086 lines) | The whole bot: config, SQLite, Chrome control, scraping, EPUB, Telegram handlers, worker loop |
| `app_autosolver.py` | Alternate build that first tries `uc_gui_click_captcha` to auto-solve Turnstile |
| `worker.py` | Worker-only entry: calls `app.worker_main()` — no Telegram polling |
| `Start for Windows.bat` | Setup + launcher for `app.py` (embeds a large PowerShell script) |
| `Start for Windows Autosolver.bat` | Same, but launches `app_autosolver.py` |
| `Worker for Windows.bat` + `worker.ps1` | Launch the worker-only mode |
| `Start for Linux.sh` | dnf-based Linux launcher (the README says Linux features live on the `seleniumbase` branch) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for settings |
| `data/` | Created at runtime: SQLite DB, library cache, Chrome profile (git-ignored) |

Runtime layout:

```
data/
├── worker.sqlite3            # tasks, novels, chapters, task_cache, settings, chapter_pulls
├── chrome-profile/           # persistent Chrome login/cookies
└── library/<novel_id>/
    ├── chapters/00001.xhtml  # cached chapter text
    ├── images/               # cached images / cover
    └── artifacts/*.epub      # finished books
```

### Key code map (`app.py`)

| What | Where |
|---|---|
| Config / `.env` loading | lines 1–180 |
| Custom exceptions (`ManualChallengeRequired`, `LoginRequired`, `ChapterLocked`, `DeadBrowser`) | [215–250](app.py#L215) |
| SQLite schema (`setup_local_db`) | [333](app.py#L333) |
| Limits (`CHAPTER_CAP`, `DAILY_TASK_LIMIT`) | [444–645](app.py#L444) |
| Task queue (insert / claim / requeue / cancel) | [645–935](app.py#L645) |
| Chapter cache | [936–1040](app.py#L936) |
| `WtrBrowser` (Chrome wrapper) | [1079](app.py#L1079) |
| Magic-link login | [1661](app.py#L1661) |
| Decrypt + glossary helpers | [1860–2180](app.py#L1860) |
| Challenge / lock detection | [2181–2260](app.py#L2181) |
| `WtrLabClient` (scraping) | [2330](app.py#L2330) |
| Cache repair | [2912–3090](app.py#L2912) |
| `build_epub` | [3137](app.py#L3137) |
| Telegram helpers (progress, send files) | [3368–3660](app.py#L3368) |
| `process_task` (main job) | [3844](app.py#L3844) |
| Telegram command handlers | [4318–4984](app.py#L4318) |
| `worker_loop` / `main` / `worker_main` | [4985–5086](app.py#L4985) |

---

## 4. Before you start

1. **A Telegram bot token.** In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. **Your Telegram numeric user ID** (optional, for admin/allow-list) — message `@userinfobot`.
3. **Windows 10/11**, local disk (not an external drive), run as a **normal user** (not administrator).

### ⚠️ State of this folder right now
I checked your folder:
- `.venv` exists but contains **only pip** — no project dependencies installed.
- It was built with **Python 3.15.0**. The project targets 3.10+ (the setup script installs **3.12**); SeleniumBase and other dependencies may not support 3.15 yet.
- There is **no `.env`** and `data/` is empty.

So you need to run setup first (Section 5A or 5B). If the install fails on 3.15, delete `.venv` and recreate it with Python 3.12 (Section 5B).

---

## 5. How to run it

### 5A. Easiest — double-click (recommended)

1. If Windows blocked the script: right-click `Start for Windows.bat` → **Properties** → tick **Unblock** → OK.
2. Double-click **`Start for Windows.bat`**.

First run, it automatically:
- downloads missing project files from GitHub,
- installs Python 3.12 if no Python 3.10+ is found,
- installs Google Chrome if missing,
- creates `.venv` and runs `pip install -r requirements.txt`,
- asks you for settings (BOT_TOKEN etc.) and writes `.env`,
- optionally closes other Chrome processes,
- starts `app.py`.

It writes `data/.setup_done`; later runs skip setup and just start the bot. To redo setup, delete `data/.setup_done` and/or `.env`.

### 5B. Manual — PowerShell commands

Run from the project folder (`D:\WTR-Lab-crawler-main\WTR-Lab-crawler-main`).

```powershell
# 1. Go to the project
cd "D:\WTR-Lab-crawler-main\WTR-Lab-crawler-main"

# 2. (Recommended) recreate the venv with Python 3.12 instead of 3.15
Remove-Item -Recurse -Force .venv
py -3.12 -m venv .venv          # if you have no 3.12:  winget install Python.Python.3.12

# 3. Install dependencies
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 4. Create your settings file
Copy-Item .env.example .env
notepad .env                    # set BOT_TOKEN=... (and optionally ADMIN_USER_IDS=your_id)

# 5. Make sure Google Chrome is installed
winget install Google.Chrome    # skip if already installed

# 6. Run the bot (queue + worker)
.\.venv\Scripts\python.exe app.py
```

Stop with **Ctrl+C**.

### 5C. Other run modes

| Goal | Command |
|---|---|
| Normal bot (manual Turnstile) | `.\.venv\Scripts\python.exe app.py` or `Start for Windows.bat` |
| Bot that tries to auto-click Turnstile | `.\.venv\Scripts\python.exe app_autosolver.py` or `Start for Windows Autosolver.bat` |
| Worker only (no Telegram polling; drains tasks already in SQLite) | `.\.venv\Scripts\python.exe worker.py` or `Worker for Windows.bat` |

Never run `app.py` and `app_autosolver.py` together — they share the bot token and Chrome profile. Close other Chrome windows if the profile is locked.

---

## 6. Using it (Telegram)

1. Open your bot in Telegram, send `/start`.
2. **First time only:** `/login` — gives the bot a logged-in WTR-Lab session in its Chrome profile. Enter your email, then paste the magic link WTR-Lab emails you.
3. `/download` → paste a link like `https://wtr-lab.com/en/novel/12345/some-slug`.
4. Tap **All chapters** or **Custom range** (type `40-60`).
5. Watch the progress message. If it says there's a captcha, tick the box in the Chrome window and send `/solved`.
6. The EPUB arrives in the chat.

### Command reference

| Command | Who | Effect |
|---|---|---|
| `/start`, `/help` | everyone | Help text |
| `/download` | everyone | Queue a novel (URL → range) |
| `/login` | everyone | Magic-link login for the shared Chrome profile |
| `/queue` | everyone | Global wait list |
| `/tasks`, `/mytasks` | everyone | Recent / your own tasks |
| `/continue` | everyone | Re-queue your latest failed task |
| `/cancel` | everyone | Cancel your pending/running tasks |
| `/status` | everyone | Worker status |
| `/cap` | everyone | Your daily limits |
| `/solved` | everyone | Resume after you solved a Turnstile |
| `/logs` | admin | Full task log with names |
| `/trial` | admin | Full task table |

`app_autosolver.py` only has `/download`, `/queue`, `/cancel`, `/cap`, `/help`; when auto-click fails you press **Enter** in the console instead of `/solved`.

---

## 7. Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | **Required.** From BotFather |
| `ALLOWED_USER_IDS` | empty | Only these Telegram IDs may use the bot. Empty = anyone who finds it (set this for a private bot!) |
| `ADMIN_USER_IDS` | empty | Exempt from limits; can use `/logs`, `/trial` |
| `ADMIN_CHAT_ID` | empty | Gets copies of finished books. If numeric and no admins set, it also becomes admin |
| `OUTPUT_GROUPS` | empty | Extra chats that get finished books |
| `CHAPTER_CAP` | 0 | Max *fresh* chapters per user per 24 h (0 = unlimited) |
| `DAILY_TASK_LIMIT` | 0 | Max new tasks per user per 24 h (0 = unlimited) |
| `CHAPTER_THROTTLE_MIN` / `MAX` | 18 / 24 in code (`.env.example` suggests 10 / 18) | Random delay between uncached chapter fetches |
| `CHAPTER_THROTTLE_SECONDS` | — | Single value, ±25 % jitter |
| `PROGRESS_UPDATE_SECONDS` | 25 in code (`.env.example` suggests 15) | How often the progress message updates |
| `HEADLESS` | 0 | `0` visible Chrome (needed for Turnstile). `1` no window |
| `CHROME_PROFILE_DIR` | `data/chrome-profile` | Chrome profile location |

Minimal `.env` for a private personal bot:

```
BOT_TOKEN=123456:ABC-your-real-token
ALLOWED_USER_IDS=111111111
ADMIN_USER_IDS=111111111
```

---

## 8. Handy maintenance commands

```powershell
# Inspect the queue (needs the sqlite3 CLI, or use DB Browser for SQLite)
sqlite3 data\worker.sqlite3 "SELECT id,status,novel_title,chapter_range,error FROM tasks ORDER BY id DESC LIMIT 20;"

# Syntax-check the code without running it
.\.venv\Scripts\python.exe -m py_compile app.py

# List installed packages
.\.venv\Scripts\python.exe -m pip list

# Force first-time setup to run again
Remove-Item data\.setup_done, .env

# Reset Chrome login/session (last resort — you must /login again)
Remove-Item -Recurse -Force data\chrome-profile

# Kill stuck Chrome processes holding the profile
Get-Process chrome | Stop-Process -Force
```

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `BOT_TOKEN is missing` | Create `.env` from `.env.example`, set the token |
| `ModuleNotFoundError` (telebot, seleniumbase…) | Install deps into the venv: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| pip errors on Python 3.15 | Recreate `.venv` with Python 3.12 (Section 5B) |
| Chrome won't open | Quit all Chrome processes; last resort delete `data\chrome-profile` and `/login` again |
| "Not allowed" in Telegram | Clear `ALLOWED_USER_IDS` or add your numeric ID |
| Stuck on captcha | Tick the box in Chrome, send `/solved` |
| Partial EPUB | Resend the same URL or `/continue` — resumes from cache |
| Two bots conflict | Run only one of `app.py` / `app_autosolver.py` |
| Telegram 409 conflict | Another instance with the same token is running somewhere — stop it |

---

## 10. Privacy & safety notes

- Never share `.env` or `data/` (contains your bot token, login cookies, database). `.gitignore` already excludes them.
- Leave `ALLOWED_USER_IDS` empty only if you're fine with anyone who finds the bot using your PC and Chrome session.
- Use it for content you're entitled to download; respect the site's terms and keep the throttle delays so you don't hammer their servers.
