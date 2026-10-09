# ═══════════════════════════════════════════════════════════
#  ⚙️  CONFIG (Railway Environment Variables)
# ═══════════════════════════════════════════════════════════
import os
import sys

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8826538267:AAGgyE3EX_pOxrxTzg7fka3fp4DGo1PU5mY")
API_ID    = os.environ.get("API_ID", "20346550")
API_HASH  = os.environ.get("API_HASH", "bc79c3bea7a626887bdc0871eecf0327")
OWNER_ID  = int(os.environ.get("OWNER_ID", "8617986101"))

MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://gogo_db_user:4DfbHqcjpjg6TYb8@firebase.snn8z2u.mongodb.net")
DB_NAME   = os.environ.get("DB_NAME", "UPLOADER_BOT")

COURSE_ID       = os.environ.get("COURSE_ID", "41")
FALLBACK_USERID = os.environ.get("FALLBACK_USERID", "464995")
AKAMAI_HOST     = os.environ.get("AKAMAI_HOST", "armathsapi.akamai.net.in")

GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    "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")

# 📢 Channel — sab files/links yahan forward honge
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1004350191024")) or None

USE_PROXY = False
PROXIES = {
    "http": os.environ.get("PROXY_HTTP", "http://apna_proxy_ip:port"),
    "https": os.environ.get("PROXY_HTTPS", "http://apna_proxy_ip:port")
}
# ═══════════════════════════════════════════════════════════

import re
import time
import json
import asyncio
import subprocess
import logging
import socket
import shutil
import zipfile
import base64
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, unquote, quote
import requests
import yt_dlp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# ─── LOGGING ───
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

INSTANCE_ID = f"{socket.gethostname()}_{os.getpid()}_{int(time.time())}"
log.info(f"🆔 Instance: {INSTANCE_ID}")

# ═══════════════════════════════════════════════════════════
#  📂 VOLUME DIRECTORIES (Railway persistent storage)
# ═══════════════════════════════════════════════════════════
VOLUME_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "data"))
VOLUME_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_DIR = VOLUME_DIR / "downloads";      DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR    = VOLUME_DIR / "thumbs";         THUMB_DIR.mkdir(exist_ok=True)
GITHUB_DIR   = VOLUME_DIR / "github_downloads"; GITHUB_DIR.mkdir(exist_ok=True)
COOKIES_FILE = VOLUME_DIR / "cookies.txt"
HISTORY_FILE = VOLUME_DIR / "downloaded_history.txt"
TOKEN_FILE   = VOLUME_DIR / "github_token.txt"

log.info(f"📂 Volume: {VOLUME_DIR.absolute()}")

# ═══════════════════════════════════════════════════════════
#  🛡️ DUPLICATE UPDATE BLOCKER
# ═══════════════════════════════════════════════════════════
_SEEN_UPDATES = {}
SEEN_TTL = 120


def is_duplicate(update_id: int) -> bool:
    now = time.time()
    for k in list(_SEEN_UPDATES.keys()):
        if now - _SEEN_UPDATES[k] > SEEN_TTL:
            _SEEN_UPDATES.pop(k, None)
    if update_id in _SEEN_UPDATES:
        return True
    _SEEN_UPDATES[update_id] = now
    return False


def channel_link(message_id: int):
    """Build t.me/c/... link from a channel message id."""
    cid = str(CHANNEL_ID)
    internal = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"


# ═══════════════════════════════════════════════════════════
#  🗄️ MONGODB
# ═══════════════════════════════════════════════════════════
users_col = captions_col = cookies_col = history_col = None
MONGO_OK = False

try:
    from pymongo import MongoClient
    _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    _client.server_info()
    _db = _client[DB_NAME]
    users_col    = _db["users"]
    captions_col = _db["captions"]
    cookies_col  = _db["cookies"]
    history_col  = _db["github_history"]
    MONGO_OK = True
    log.info(f"✅ MongoDB connected: {DB_NAME}")
except Exception as e:
    log.error(f"❌ MongoDB fail: {e}")

# ═══════════════════════════════════════════════════════════
#  🔧 LOCAL BOT API SERVER (Optional — 4GB mode)
# ═══════════════════════════════════════════════════════════
LOCAL_API_PORT = int(os.environ.get("LOCAL_API_PORT", "8081"))
LOCAL_API_DIR  = os.environ.get("LOCAL_API_DIR", "/tmp/tg-bot-api")
os.makedirs(LOCAL_API_DIR, exist_ok=True)

# Railway internal URL support
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "").rstrip("/")


def find_local_api_binary():
    candidates = [
        "/usr/local/bin/telegram-bot-api",
        "/app/bin/telegram-bot-api",
        "bin/telegram-bot-api",
        "./bin/telegram-bot-api",
        "telegram-bot-api",
    ]
    for binary in candidates:
        p = binary if binary.startswith("/") else shutil.which(binary)
        if p and os.path.isfile(p):
            try:
                os.chmod(p, 0o755)
            except Exception:
                pass
            if os.access(p, os.X_OK):
                return p
    return None


def kill_existing_api():
    try:
        subprocess.run(["pkill", "-f", "telegram-bot-api"],
                       capture_output=True, timeout=5)
        time.sleep(1)
    except Exception:
        pass


def auto_logout_public_api():
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/logOut",
                         timeout=10)
        if r.json().get("ok"):
            log.info("✅ Public API logout")
    except Exception as e:
        log.warning(f"⚠️ Logout fail: {e}")


def start_local_api_server():
    # If external local API URL provided (Railway separate service)
    if LOCAL_API_URL:
        log.info(f"🌐 Using external Local API: {LOCAL_API_URL}")
        try:
            r = requests.get(f"{LOCAL_API_URL}/bot{BOT_TOKEN}/getMe",
                             timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                log.info("✅ External Local API ready — 4GB mode ON")
                return True
            log.warning(f"⚠️ External API check failed: {r.status_code}")
        except Exception as e:
            log.warning(f"⚠️ External API unreachable: {e}")
        return False

    log.info("🚀 Local Bot API Server check...")
    binary_path = find_local_api_binary()
    if not binary_path:
        log.warning("⚠️ telegram-bot-api binary NOT found — 50MB mode.")
        return False
    log.info(f"✅ Found binary: {binary_path}")
    kill_existing_api()
    auto_logout_public_api()
    try:
        subprocess.Popen(
            [binary_path, f"--api-id={API_ID}", f"--api-hash={API_HASH}",
             "--local", f"--http-port={LOCAL_API_PORT}",
             f"--dir={LOCAL_API_DIR}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
        try:
            r = requests.get(
                f"http://localhost:{LOCAL_API_PORT}/bot{BOT_TOKEN}/getMe",
                timeout=5)
            if r.status_code == 200:
                log.info("✅ Local API ready — 4GB mode ON")
                return True
        except Exception:
            pass
    except Exception as e:
        log.warning(f"⚠️ Local API start fail: {e}")
    log.warning("⚠️ Local API nahi chala — 50MB mode")
    return False


LOCAL_API_OK = start_local_api_server()
USE_LOCAL_API = LOCAL_API_OK

if LOCAL_API_OK:
    if LOCAL_API_URL:
        LOCAL_API_BASE  = f"{LOCAL_API_URL}/bot"
        LOCAL_FILE_BASE = f"{LOCAL_API_URL}/file/bot"
    else:
        LOCAL_API_BASE  = f"http://localhost:{LOCAL_API_PORT}/bot"
        LOCAL_FILE_BASE = f"http://localhost:{LOCAL_API_PORT}/file/bot"
    MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2000"))
else:
    LOCAL_API_BASE  = "https://api.telegram.org/bot"
    LOCAL_FILE_BASE = "https://api.telegram.org/file/bot"
    MAX_UPLOAD_MB   = 50

# ─── WAITING STATES ───
WAITING_TXT            = {}
WAITING_CAPTION        = {}
WAITING_GITHUB         = {}
WAITING_GITHUB_PROFILE = {}
WAITING_GITHUB_SEARCH  = {}
WAITING_GITHUB_USER    = {}
WAITING_GITHUB_FILE    = {}
WAITING_GITHUB_CODE    = {}
WAITING_PREMIUM_ADD    = {}
WAITING_PREMIUM_REMOVE = {}
WAITING_URL            = {}
STOP_FLAGS      = {}
CURRENT_TASK    = {}

MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv",
              ".mp3", ".m4a", ".pdf", ".zip", ".rar", ".ts", ".apk")

DEFAULT_CAPTION = (
    "[📁] File_ID : {file_index}\n\n"
    "NAME  : {file_name}\n\n"
    "💼  Size : {file_size}\n\n"
    "📚 BATCH NAME : {batch_name}\n\n"
    "DOWNLOADED BY : {downloaded_by} ❤️"
)

# ═══════════════════════════════════════════════════════════
#  🐙 GITHUB — Token Management
# ═══════════════════════════════════════════════════════════
_RUNTIME_TOKEN = {"value": None}


def get_github_token():
    if _RUNTIME_TOKEN["value"]:
        return _RUNTIME_TOKEN["value"]
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text().strip()
            if t:
                return t
    except Exception:
        pass
    return GITHUB_TOKEN


def save_github_token(token):
    _RUNTIME_TOKEN["value"] = token.strip()
    try:
        TOKEN_FILE.write_text(token.strip())
    except Exception:
        pass


def github_headers():
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Mozilla/5.0 (CourseUploaderBot)"
    }
    tk = get_github_token()
    if tk:
        h["Authorization"] = f"token {tk}"
    return h


def get_requests_kwargs():
    kwargs = {}
    if USE_PROXY:
        kwargs["proxies"] = PROXIES
    return kwargs


def esc(text):
    if not text:
        return ""
    return (str(text).replace("\\", "\\\\").replace("`", "\\`")
            .replace("*", "\\*").replace("_", "\\_")
            .replace("[", "\\[").replace("]", "\\]"))


# ═══════════════════════════════════════════════════════════
#  📜 GITHUB HISTORY
# ═══════════════════════════════════════════════════════════
def load_history():
    if MONGO_OK:
        try:
            docs = history_col.find({}, {"_id": 1})
            return set(d["_id"] for d in docs)
        except Exception:
            pass
    if not HISTORY_FILE.exists():
        return set()
    try:
        return set(line.strip() for line in
                   HISTORY_FILE.read_text(encoding="utf-8").splitlines()
                   if line.strip())
    except Exception:
        return set()


def save_to_history(repo_name):
    if MONGO_OK:
        try:
            history_col.update_one({"_id": repo_name},
                                   {"$set": {"ts": datetime.now(timezone.utc)}},
                                   upsert=True)
            return
        except Exception:
            pass
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(repo_name + "\n")
    except Exception:
        pass


def clear_history():
    if MONGO_OK:
        try:
            history_col.delete_many({})
        except Exception:
            pass
    try:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  🐙 GITHUB DOWNLOADER
# ═══════════════════════════════════════════════════════════
def is_github_profile_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if "github.com" not in p.netloc.lower():
            return False
        parts = [x for x in p.path.strip("/").split("/") if x]
        if len(parts) == 1:
            reserved = {"orgs", "settings", "notifications", "explore",
                        "marketplace", "pricing", "features", "about",
                        "login", "signup", "new", "topics", "trending",
                        "search", "sponsors"}
            if parts[0].lower() in reserved:
                return False
            return True
        return False
    except Exception:
        return False


def extract_github_username(url: str) -> str:
    try:
        m = re.search(r"github\.com/([^/?#]+)", url)
        if m:
            return m.group(1).strip()
        p = urlparse(url)
        parts = [x for x in p.path.strip("/").split("/") if x]
        return parts[0] if parts else ""
    except Exception:
        return ""


def fetch_all_repos(username: str):
    repos = []
    page = 1
    per_page = 100
    while True:
        url = (f"https://api.github.com/users/{username}/repos"
               f"?per_page={per_page}&page={page}&sort=created&direction=asc")
        try:
            r = requests.get(url, headers=github_headers(),
                             timeout=20, **get_requests_kwargs())
            if r.status_code == 404:
                return None, "User nahi mila"
            if r.status_code == 403 or r.status_code == 429:
                reset = int(r.headers.get("X-RateLimit-Reset",
                                          time.time() + 3600))
                wait = max(0, reset - int(time.time())) + 5
                return None, f"Rate limit — {wait // 60} min baad try karo"
            if r.status_code != 200:
                return None, f"GitHub API error: {r.status_code}"
            data = r.json()
            if not data:
                break
            repos.extend(data)
            if len(data) < per_page:
                break
            page += 1
            if page > 50:
                break
        except Exception as e:
            return None, f"Network error: {str(e)[:100]}"
    return repos, None


def format_size_kb(kb):
    if kb < 1024:
        return f"{kb:.2f} KB"
    return f"{kb / 1024:.2f} MB"


def parse_range(text: str, total: int):
    text = text.strip().replace(" ", "")
    if not text:
        return None
    if "," in text:
        try:
            idxs = sorted(set(int(x) for x in text.split(",") if x))
            return [i for i in idxs if 1 <= i <= total]
        except Exception:
            return None
    m = re.match(r"^(\d+)-(\d+)$", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a < 1: a = 1
        if b > total: b = total
        if a > b: return None
        return (a, b)
    m = re.match(r"^(\d+)$", text)
    if m:
        b = int(m.group(1))
        if b < 1: return None
        if b > total: b = total
        return (1, b)
    return None


def download_github_repo(repo: dict, out_dir: Path, progress_cb=None):
    name = repo["name"]
    owner = repo["owner"]["login"]
    default_branch = repo.get("default_branch", "main")
    zip_url = (f"https://codeload.github.com/{owner}/{name}/zip/"
               f"refs/heads/{default_branch}")
    zip_path = out_dir / f"{name}.zip"
    try:
        r = requests.get(zip_url, headers=github_headers(),
                         stream=True, timeout=120, **get_requests_kwargs())
        if r.status_code != 200:
            zip_url = (f"https://codeload.github.com/{owner}/{name}/zip/"
                       f"refs/heads/master")
            r = requests.get(zip_url, headers=github_headers(),
                             stream=True, timeout=120, **get_requests_kwargs())
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
        return zip_path, None
    except Exception as e:
        return None, str(e)[:100]


def get_zip_size_mb(zip_path: Path) -> float:
    return zip_path.stat().st_size / (1024 * 1024)


# ═══════════════════════════════════════════════════════════
#  📢 FORWARD TO CHANNEL
# ═══════════════════════════════════════════════════════════
async def forward_to_channel(ctx, source_chat_id, message_id):
    if not CHANNEL_ID:
        return None
    try:
        fwd = await ctx.bot.forward_message(
            chat_id=CHANNEL_ID,
            from_chat_id=source_chat_id,
            message_id=message_id,
            read_timeout=120,
            write_timeout=120)
        return channel_link(fwd.message_id)
    except Exception as e:
        log.warning(f"⚠️ Forward to channel fail: {e}")
        try:
            await ctx.bot.send_message(
                CHANNEL_ID,
                f"🔗 File uploaded by user in bot (forward failed).\n"
                f"Msg: `{source_chat_id}/{message_id}`",
                parse_mode="Markdown")
        except Exception:
            pass
        return None


async def send_channel_notice(ctx, text):
    if not CHANNEL_ID:
        return
    try:
        await ctx.bot.send_message(CHANNEL_ID, text, parse_mode="Markdown",
                                   disable_web_page_preview=True)
    except Exception as e:
        log.warning(f"Channel notice fail: {e}")


# ═══════════════════════════════════════════════════════════
#  🐙 GITHUB — Profile show + range prompt
# ═══════════════════════════════════════════════════════════
async def github_show_repos(update, ctx, url):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_owner(uid):
        ok_p, _ = is_premium(uid)
        if not ok_p:
            await update.message.reply_text(
                f"🚫 *Access Denied*\n\nID: `{uid}`",
                parse_mode="Markdown")
            return

    username = extract_github_username(url)
    if not username:
        await update.message.reply_text("❌ GitHub username detect nahi hua.")
        return

    msg = await update.message.reply_text(
        f"🔍 *Fetching repos for* `{username}` ...",
        parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    repos, err = await loop.run_in_executor(None, fetch_all_repos, username)

    if err:
        await msg.edit_text(f"❌ {err}")
        return
    if not repos:
        await msg.edit_text(f"📭 `{username}` ke koi public repos nahi.")
        return

    downloaded = load_history()
    new_repos = [r for r in repos if r["name"] not in downloaded]
    total_kb = sum((r.get("size") or 0) for r in repos)
    total_mb = total_kb / 1024

    summary = (
        f"👤 `{username}`\n\n"
        f"📦 Total repos: {len(repos)}\n"
        f"🆕 New (not downloaded): {len(new_repos)}\n"
        f"💾 Approx: ~{total_mb:.0f} MB\n\n"
        f"❓ *Kaunse repos chahiye?*\n\n"
        f"Range format:\n"
        f"• `1-50` → repos 1 se 50\n"
        f"• `500-600` → repos 500 se 600\n"
        f"• `2000-2100` → repos 2000 se 2100\n"
        f"• `200` → repos 1 se 200\n"
        f"• `1,5,10` → specific repos\n\n"
        f"Ab range bhejo:"
    )
    await msg.edit_text(summary, parse_mode="Markdown")

    WAITING_GITHUB[uid] = {
        "username": username,
        "repos": repos,
        "chat_id": chat_id,
        "prompt_msg_id": msg.message_id,
        "started_at": time.time(),
    }


async def github_process_range(update, ctx, uid, text):
    state = WAITING_GITHUB.get(uid)
    if not state:
        return False
    repos = state["repos"]
    total = len(repos)
    parsed = parse_range(text, total)
    if parsed is None:
        await update.message.reply_text(
            "❌ Invalid range. Examples: `1-50`, `500-600`, `200`, `1,5,10`",
            parse_mode="Markdown")
        return True
    if isinstance(parsed, list):
        indexes = parsed
    else:
        a, b = parsed
        indexes = list(range(a, b + 1))
    if not indexes:
        await update.message.reply_text("❌ Koi repo select nahi hua.")
        WAITING_GITHUB.pop(uid, None)
        return True
    username = state["username"]
    WAITING_GITHUB.pop(uid, None)
    await update.message.reply_text(
        f"🚀 *Starting GitHub download*\n\n"
        f"👤 `{username}`\n"
        f"📊 Range: #{indexes[0]} → #{indexes[-1]}\n"
        f"📦 Total selected: {len(indexes)}\n\n"
        f"`/stop` bhej ke rok sakte ho.",
        parse_mode="Markdown")
    task = asyncio.create_task(
        github_download_batch(update, ctx, username, repos, indexes))
    CURRENT_TASK[uid] = task

    def _done(t):
        if CURRENT_TASK.get(uid) is t:
            CURRENT_TASK.pop(uid, None)
        STOP_FLAGS.pop(uid, None)

    task.add_done_callback(_done)
    return True


async def github_download_batch(update, ctx, username, repos, indexes):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    STOP_FLAGS[uid] = False
    send_ok = 0
    skipped = 0
    failed = 0
    loop = asyncio.get_event_loop()

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{uid}")]
    ])

    status = await ctx.bot.send_message(
        chat_id,
        f"🐙 *GitHub Batch*\n\n"
        f"👤 `{username}`\n"
        f"📦 {len(indexes)} repos\n\n"
        f"Shuru kar raha hoon...",
        parse_mode="Markdown", reply_markup=cancel_kb)

    for counter, repo_idx in enumerate(indexes, 1):
        if is_stopped(uid):
            break
        repo = repos[repo_idx - 1]
        repo_name = repo["name"]
        repo_url = repo["html_url"]
        size_kb = repo.get("size") or 0
        size_str = format_size_kb(size_kb)

        await safe_edit(
            ctx.bot, chat_id, status.message_id,
            f"📦 Item {counter}/{len(indexes)}\n"
            f"🐙 {repo_name}\n\n"
            f"⬇️ Downloading ZIP...", cancel_kb)

        def _prog(done, total):
            pass

        zip_path, err = await loop.run_in_executor(
            None, download_github_repo, repo, GITHUB_DIR, _prog)

        if err or not zip_path:
            failed += 1
            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"❌ #{repo_idx:02d} `{repo_name}` — fail\n`{err}`",
                    parse_mode="Markdown")
            except Exception:
                pass
            continue
        if is_stopped(uid):
            try: zip_path.unlink()
            except: pass
            break
        size_mb = get_zip_size_mb(zip_path)

        if size_mb > MAX_UPLOAD_MB:
            failed += 1
            link = repo_url
            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"⚠️ #{repo_idx:02d} `{repo_name}` skip — "
                    f"{size_mb:.1f} MB > {MAX_UPLOAD_MB} MB\n"
                    f"🔗 [Open Repo]({link})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True)
            except Exception:
                pass
            await send_channel_notice(
                ctx,
                f"📁 *Repo:* `{repo_name}`\n"
                f"👤 *User:* `{username}`\n"
                f"⚠️ >{MAX_UPLOAD_MB}MB — direct link\n"
                f"🔗 {link}")
            try: zip_path.unlink()
            except: pass
            continue

        display_name = f"{repo_idx:02d}_{repo_name}.zip"
        caption = (
            f"📦 #{repo_idx:02d} — {repo_name}\n"
            f"🔗 {repo_url}\n"
            f"💾 {size_str}"
        )
        sent_msg = None
        try:
            with open(zip_path, "rb") as f:
                sent_msg = await ctx.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=display_name,
                    caption=caption,
                    read_timeout=7200,
                    write_timeout=7200)
            send_ok += 1
            save_to_history(repo_name)

            if sent_msg:
                ch_link = await forward_to_channel(ctx, chat_id,
                                                   sent_msg.message_id)
                if ch_link:
                    try:
                        await ctx.bot.send_message(
                            chat_id,
                            f"📢 *Channel me bhi bhej diya!*\n"
                            f"🔗 [View in Channel]({ch_link})",
                            parse_mode="Markdown",
                            disable_web_page_preview=True)
                    except Exception:
                        pass
        except Exception as e:
            failed += 1
            log.warning(f"GitHub upload fail {repo_name}: {e}")
        finally:
            try: zip_path.unlink()
            except: pass
        await asyncio.sleep(0.5)

    final = (
        f"✅ *DONE*\n\n"
        f"📊 Range: #{indexes[0]} → #{indexes[-1]}\n"
        f"✅ Bheje: {send_ok}\n"
        f"⏭️ Skip: {skipped}\n"
        f"❌ Fail: {failed}\n"
        f"📦 Total: {len(indexes)}"
    )
    await safe_edit(ctx.bot, chat_id, status.message_id, final)


# ═══════════════════════════════════════════════════════════
#  🐙 GITHUB — Search / User Info / File / Code
# ═══════════════════════════════════════════════════════════
def _github_access(update) -> bool:
    uid = update.effective_user.id
    if is_owner(uid):
        return True
    ok_p, _ = is_premium(uid)
    return ok_p


async def github_search_cmd(update, ctx):
    if not _github_access(update): return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/search <keyword>`\nExample: `/search python bot`",
            parse_mode="Markdown"); return
    keyword = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(
        f"🔍 Searching repos for: `{esc(keyword)}`...",
        parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(
                f"https://api.github.com/search/repositories"
                f"?q={quote(keyword)}&per_page=20&sort=stars",
                headers=github_headers(), timeout=20, **get_requests_kwargs())
            if r.status_code != 200:
                return None, f"Search failed (status {r.status_code})"
            return r.json().get("items", []), None
        except Exception as e:
            return None, str(e)[:120]

    items, err = await loop.run_in_executor(None, _do)
    if err:
        await msg.edit_text(f"❌ {err}"); return
    if not items:
        await msg.edit_text("❌ No repos found."); return

    text = f"🔍 *Top {len(items)} repos for:* `{esc(keyword)}`\n\n"
    for i, repo in enumerate(items, 1):
        desc = esc((repo.get("description") or "No description")[:100])
        text += (f"*{i}. {esc(repo['full_name'])}* ⭐{repo['stargazers_count']}\n"
                 f"📝 {desc}\n🔗 {repo['html_url']}\n\n")
    for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk)


async def github_user_cmd(update, ctx):
    if not _github_access(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/user <username>`",
                                        parse_mode="Markdown"); return
    username = ctx.args[0].strip().lstrip("@")
    msg = await update.message.reply_text(f"🔍 Fetching `{esc(username)}`...",
                                          parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(f"https://api.github.com/users/{username}",
                             headers=github_headers(), timeout=20,
                             **get_requests_kwargs())
            if r.status_code != 200:
                return None, f"User not found (status {r.status_code})"
            return r.json(), None
        except Exception as e:
            return None, str(e)[:120]

    u, err = await loop.run_in_executor(None, _do)
    if err:
        await msg.edit_text(f"❌ {err}"); return

    text = (
        f"👤 *{esc(u.get('login'))}*\n"
        f"📝 Name: {esc(u.get('name') or 'N/A')}\n"
        f"🏢 Company: {esc(u.get('company') or 'N/A')}\n"
        f"📍 Location: {esc(u.get('location') or 'N/A')}\n"
        f"📧 Email: {esc(u.get('email') or 'N/A')}\n"
        f"🔗 Blog: {esc(u.get('blog') or 'N/A')}\n"
        f"📦 Public Repos: {u.get('public_repos')}\n"
        f"👥 Followers: {u.get('followers')} | Following: {u.get('following')}\n"
        f"🔗 {u.get('html_url')}"
    )
    await msg.edit_text(text, parse_mode="Markdown",
                        disable_web_page_preview=True)


async def github_file_cmd(update, ctx):
    if not _github_access(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/file <filename>`",
                                        parse_mode="Markdown"); return
    keyword = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(
        f"🔍 Searching files for: `{esc(keyword)}`...",
        parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(
                f"https://api.github.com/search/code"
                f"?q=filename:{quote(keyword)}&per_page=20",
                headers=github_headers(), timeout=20, **get_requests_kwargs())
            if r.status_code != 200:
                return None, (f"File search failed (status {r.status_code}). "
                              f"Valid token required.")
            return r.json().get("items", []), None
        except Exception as e:
            return None, str(e)[:120]

    items, err = await loop.run_in_executor(None, _do)
    if err:
        await msg.edit_text(f"❌ {err}"); return
    if not items:
        await msg.edit_text("❌ No files found."); return

    text = f"📁 *Files matching:* `{esc(keyword)}`\n\n"
    for i, item in enumerate(items, 1):
        text += (f"*{i}. {esc(item['name'])}*\n"
                 f"📂 Repo: {esc(item['repository']['full_name'])}\n"
                 f"📄 Path: `{esc(item['path'])}`\n"
                 f"🔗 {item['html_url']}\n\n")
    for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk)


async def github_code_cmd(update, ctx):
    if not _github_access(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/code <keyword>`",
                                        parse_mode="Markdown"); return
    keyword = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(
        f"🔍 Searching code for: `{esc(keyword)}`...",
        parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(
                f"https://api.github.com/search/code?q={quote(keyword)}&per_page=20",
                headers=github_headers(), timeout=20, **get_requests_kwargs())
            if r.status_code != 200:
                return None, (f"Code search failed (status {r.status_code}). "
                              f"Valid token required.")
            return r.json().get("items", []), None
        except Exception as e:
            return None, str(e)[:120]

    items, err = await loop.run_in_executor(None, _do)
    if err:
        await msg.edit_text(f"❌ {err}"); return
    if not items:
        await msg.edit_text("❌ No code matches found."); return

    text = f"💻 *Code matching:* `{esc(keyword)}`\n\n"
    for i, item in enumerate(items, 1):
        text += (f"*{i}. {esc(item['name'])}*\n"
                 f"📂 {esc(item['repository']['full_name'])}\n"
                 f"📄 `{esc(item['path'])}`\n"
                 f"🔗 {item['html_url']}\n\n")
    for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk)


async def github_replacetoken_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only command."); return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/replacetoken <github_token>`", parse_mode="Markdown")
        return
    new_token = ctx.args[0].strip()
    save_github_token(new_token)
    await update.message.reply_text(
        f"✅ Token updated.\nToken: `{new_token[:8]}...{new_token[-4:]}`",
        parse_mode="Markdown")


async def github_tokenstatus_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only command."); return
    tk = get_github_token()
    if not tk:
        await update.message.reply_text(
            "⚠️ No token set. Use `/replacetoken <token>`",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"🔑 Current token: `{tk[:8]}...{tk[-4:]}`",
            parse_mode="Markdown")


async def github_history_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only command."); return
    if ctx.args and ctx.args[0].lower() in ("clear", "reset", "wipe"):
        clear_history()
        await update.message.reply_text("✅ History cleared.")
        return
    hist = load_history()
    if not hist:
        await update.message.reply_text("📭 History empty.")
        return
    lines = [f"📜 *GitHub Download History* ({len(hist)} items)\n"]
    for i, name in enumerate(sorted(hist)[:80], 1):
        lines.append(f"{i}. `{name}`")
    if len(hist) > 80:
        lines.append(f"\n_...aur {len(hist) - 80} items_")
    lines.append("\n\nUse `/ghhistory clear` to wipe.")
    full = "\n".join(lines)
    for chunk in [full[i:i+3500] for i in range(0, len(full), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


# ═══════════════════════════════════════════════════════════
#  🍪 COOKIES (MongoDB-backed)
# ═══════════════════════════════════════════════════════════
def save_cookies_to_db(content: bytes, filename: str = "cookies.txt") -> bool:
    if not MONGO_OK: return False
    try:
        cookies_col.update_one(
            {"_id": "primary"},
            {"$set": {"content": content, "filename": filename,
                      "updated_at": datetime.now(timezone.utc),
                      "size": len(content)}},
            upsert=True)
        return True
    except Exception as e:
        log.warning(f"⚠️ save cookies: {e}")
        return False


def load_cookies_from_db() -> dict:
    if not MONGO_OK: return {}
    try:
        return cookies_col.find_one({"_id": "primary"}) or {}
    except Exception:
        return {}


def delete_cookies_from_db() -> bool:
    if not MONGO_OK: return False
    try:
        return cookies_col.delete_one({"_id": "primary"}).deleted_count > 0
    except Exception:
        return False


def ensure_cookies_file() -> bool:
    data = load_cookies_from_db()
    content = data.get("content")
    if not content:
        return False
    try:
        COOKIES_FILE.write_bytes(content)
        return True
    except Exception:
        return False


if MONGO_OK and ensure_cookies_file():
    log.info("✅ Cookies loaded from MongoDB")

# ═══════════════════════════════════════════════════════════
#  🗄️ DB HELPERS (Premium)
# ═══════════════════════════════════════════════════════════
def now_utc():
    return datetime.now(timezone.utc)


def is_premium(user_id):
    if not MONGO_OK: return False, 0
    try:
        u = users_col.find_one({"_id": str(user_id)})
        if not u: return False, 0
        exp = u.get("expires")
        if not exp: return False, 0
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = now_utc()
        if exp < now: return False, 0
        return True, (exp - now).days
    except Exception:
        return False, 0


def add_premium(user_id, days, added_by):
    if not MONGO_OK: return "DB error"
    try:
        uid = str(user_id); now = now_utc()
        u = users_col.find_one({"_id": uid})
        base = now
        if u and u.get("expires"):
            e = u["expires"]
            if e.tzinfo is None: e = e.replace(tzinfo=timezone.utc)
            if e > now: base = e
        new_exp = base + timedelta(days=days)
        users_col.update_one(
            {"_id": uid},
            {"$set": {"expires": new_exp, "added_by": str(added_by),
                      "added_at": now}},
            upsert=True)
        return new_exp.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return "DB error"


def remove_premium(user_id):
    if not MONGO_OK: return False
    try:
        return users_col.delete_one({"_id": str(user_id)}).deleted_count > 0
    except Exception:
        return False


def list_premium_users():
    if not MONGO_OK: return []
    try:
        return list(users_col.find({}))
    except Exception:
        return []


def is_owner(user_id):
    return int(user_id) == int(OWNER_ID)


def get_user_caption(uid):
    default = {"enabled": True, "template": DEFAULT_CAPTION}
    if not MONGO_OK: return default
    try:
        c = captions_col.find_one({"_id": str(uid)})
        if not c: return default
        return {"enabled": c.get("enabled", True),
                "template": c.get("template", DEFAULT_CAPTION)}
    except Exception:
        return default


def set_caption_enabled(uid, enabled):
    if not MONGO_OK: return
    try:
        captions_col.update_one({"_id": str(uid)},
                                {"$set": {"enabled": enabled}}, upsert=True)
    except Exception:
        pass


def set_caption_template(uid, template):
    if not MONGO_OK: return
    try:
        captions_col.update_one({"_id": str(uid)},
                                {"$set": {"template": template}}, upsert=True)
    except Exception:
        pass


def reset_caption(uid):
    if not MONGO_OK: return
    try:
        captions_col.update_one({"_id": str(uid)},
                                {"$set": {"template": DEFAULT_CAPTION}},
                                upsert=True)
    except Exception:
        pass


def render_caption(template, values):
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out

# ═══════════════════════════════════════════════════════════
#  🎯 URL HANDLING
# ═══════════════════════════════════════════════════════════
def is_direct_media_url(url):
    low = url.lower().split("?")[0]
    return any(low.endswith(e) for e in MEDIA_EXTS)


def is_api_url(url):
    low = url.lower()
    return "fetch_video" in low or ("video_id=" in low and "token=" in low)


def build_candidate_urls(video_id, course_id, token, userid):
    vid, cid = str(video_id), str(course_id)
    return [
        f"https://{AKAMAI_HOST}/{cid}/{vid}.mp4",
        f"https://{AKAMAI_HOST}/videos/{cid}/{vid}.mp4",
        f"https://{AKAMAI_HOST}/video/{cid}/{vid}.mp4",
        f"https://{AKAMAI_HOST}/media/{cid}/{vid}.mp4",
        f"https://{AKAMAI_HOST}/hls/{cid}/{vid}.m3u8",
    ]


def is_url_live(url, timeout=3):
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code in (200, 206)
    except Exception:
        return False


def find_working_url(video_id, course_id, token, userid, user_id=None):
    for url in build_candidate_urls(video_id, course_id, token, userid):
        if user_id and is_stopped(user_id):
            raise yt_dlp.utils.DownloadError("User stopped")
        if is_url_live(url):
            return url
    raise ValueError("Koi URL pattern kaam nahi kiya")


def resolve_url(url, user_id=None):
    if is_direct_media_url(url): return url
    if is_api_url(url):
        p = urlparse(url); qs = parse_qs(p.query)
        video_id  = qs.get("video_id",  [""])[0]
        token     = qs.get("token",     [""])[0]
        userid    = qs.get("userid",    [FALLBACK_USERID])[0]
        course_id = qs.get("course_id", [COURSE_ID])[0]
        if video_id and token:
            return find_working_url(video_id, course_id, token, userid, user_id)
    return url

# ═══════════════════════════════════════════════════════════
#  🖼️ THUMBNAIL + DURATION
# ═══════════════════════════════════════════════════════════
def generate_thumbnail(video_path, out_path, seek_sec=5):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(seek_sec), "-i", str(video_path),
             "-vframes", "1", "-vf", "scale=320:-1", "-q:v", "5",
             str(out_path)],
            capture_output=True, timeout=30)
        if out_path.exists() and out_path.stat().st_size > 0: return True
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vframes", "1",
             "-vf", "scale=320:-1", "-q:v", "5", str(out_path)],
            capture_output=True, timeout=30)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


def get_media_info(path):
    info = {"duration": 0.0, "width": 0, "height": 0}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=20, text=True)
        try: info["duration"] = float(r.stdout.strip())
        except Exception: pass
        if info["duration"] <= 0:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, timeout=20, text=True)
            try: info["duration"] = float(r.stdout.strip())
            except Exception: pass
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, timeout=20, text=True)
        out = r.stdout.strip()
        if "x" in out:
            p = out.split("x")
            info["width"] = int(p[0]); info["height"] = int(p[1])
    except Exception:
        pass
    return info

# ───────── HELPERS ─────────
def fmt_size(b):
    b = float(b or 0)
    if b < 1024: return f"{b:.0f} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def fmt_size_mib(b):
    b = float(b or 0)
    mib = b / (1024 * 1024)
    if mib < 1024: return f"{mib:.2f} MiB"
    return f"{mib / 1024:.2f} GiB"


def fmt_speed_mib(bps): return fmt_size_mib(bps) + "/s"


def fmt_eta_fancy(secs):
    if secs is None or secs < 0 or secs == float("inf"):
        return "Calculating..."
    secs = int(secs)
    if secs < 60: return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60: return f"{m}m, {s}s"
    h, m = divmod(m, 60)
    return f"{h}h, {m}m"


def fmt_duration(secs):
    if not secs or secs <= 0: return "0:00"
    secs = int(secs)
    h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
    if h: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def make_bar(pct, width=20):
    filled = int(width * pct / 100)
    filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)


def build_fancy_progress(phase, done, total, speed, header=""):
    pct = (done / total * 100) if total else 0.0
    bar = make_bar(pct, 20)
    eta = fmt_eta_fancy((total - done) / speed
                        if speed and total > done else None)
    lines = []
    if header:
        lines.append(header); lines.append("")
    lines.append(f"┌─「 {phase} 」─○"); lines.append("│")
    lines.append(f"│ » Progress:- {pct:.2f}%"); lines.append("│")
    lines.append(f"│ » {bar}"); lines.append("│")
    lines.append(f"│ » «{fmt_size_mib(done)} of {fmt_size_mib(total)}»")
    lines.append("│")
    lines.append(f"│ » Speed:- {fmt_speed_mib(speed)}"); lines.append("│")
    lines.append(f"│ » ETA:- {eta}"); lines.append("│")
    lines.append("└────────────────────○")
    return "\n".join(lines)


async def safe_edit(bot, chat_id, msg_id, text, keyboard=None):
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=text[:4000], reply_markup=keyboard)
    except Exception:
        pass


def is_stopped(user_id): return STOP_FLAGS.get(user_id, False)

# ───────── TXT PARSER ─────────
URL_RE   = re.compile(r"(https?://\S+)")
TYPE_RE  = re.compile(r"\[(VIDEO|PDF)\]", re.I)
SHORT_RE = re.compile(
    r"^\s*(?P<vid>\d+)\s*[\|,]\s*(?P<tok>eyJ[\w\-\.]+)"
    r"\s*(?:[\|,]\s*(?P<uid>\d+))?\s*$")


def url_basename(url):
    clean = url.split("?")[0].split("#")[0]
    name = unquote(clean.rstrip("/").split("/")[-1])
    return name or "file"


def parse_txt(text):
    items = []; pending_name = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line: continue
        um = URL_RE.search(line)
        if um:
            url = um.group(1).rstrip(").,;\"'")
            before = line[:um.start()].strip().rstrip(":").strip()
            tm = TYPE_RE.search(before)
            ftype = tm.group(1).upper() if tm else "VIDEO"
            name_part = TYPE_RE.sub("", before).strip()
            name = name_part if name_part else (pending_name or url_basename(url))
            name = name.rstrip(":").strip() or url_basename(url)
            items.append({"group": "", "type": ftype, "name": name,
                          "url": url, "mode": "url"})
            pending_name = None; continue
        sm = SHORT_RE.match(line)
        if sm:
            items.append({"group": "", "type": "VIDEO",
                          "name": f"Video {sm.group('vid')}",
                          "video_id": sm.group("vid"),
                          "token": sm.group("tok"),
                          "userid": sm.group("uid"), "mode": "short"})
            pending_name = None; continue
        pending_name = line.rstrip(":").strip()
    return items

# ═══════════════════════════════════════════════════════════
#  📥 DOWNLOAD
# ═══════════════════════════════════════════════════════════
def download_file(url, out_dir, base, info, user_id=None):
    def hook(d):
        if d.get("status") == "downloading":
            info["done"]  = d.get("downloaded_bytes", 0)
            info["total"] = d.get("total_bytes") or \
                            d.get("total_bytes_estimate", 0)
            t = time.time()
            if info.get("_t"):
                dt = t - info["_t"]
                if dt >= 0.5:
                    info["speed"] = (info["done"] - info["_b"]) / dt
                    info["_t"], info["_b"] = t, info["done"]
            else:
                info["_t"], info["_b"] = t, info["done"]
            if user_id and is_stopped(user_id):
                raise yt_dlp.utils.DownloadError("User stopped")

    ensure_cookies_file()
    has_cookies = COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0

    browser_headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://{urlparse(url).netloc}/",
    }

    opts = {
        "outtmpl": str(out_dir / f"{base}.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "progress_hooks": [hook],
        "retries": 5, "fragment_retries": 5,
        "socket_timeout": 30,
        "http_headers": browser_headers,
        "cookiefile": str(COOKIES_FILE) if has_cookies else None,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_safari", "web_creator",
                                  "tv_embedded", "ios", "android"],
                "player_skip": ["webpage", "configs"],
                "skip": ["hls", "dash"],
            }
        },
        "nocheckcertificate": True,
        "geo_bypass": True,
        "postprocessors": [{
            "key": "FFmpegVideoRemuxer",
            "preferedformat": "mp4",
        }],
    }

    final_path = None
    with yt_dlp.YoutubeDL(opts) as y:
        result = y.extract_info(url, download=True)

        if result.get("requested_downloads"):
            rd = result["requested_downloads"][0]
            fp = rd.get("filepath")
            if fp and os.path.exists(fp):
                final_path = fp

        if not final_path:
            for ext in [".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".mov"]:
                p = out_dir / f"{base}{ext}"
                if p.exists() and p.stat().st_size > 10 * 1024:
                    final_path = str(p); break

        if not final_path:
            all_files = [
                f for f in out_dir.glob(f"{base}.*")
                if f.is_file() and not f.name.endswith(".part")
                and ".f" not in f.name
            ]
            if all_files:
                all_files.sort(key=lambda x: x.stat().st_size, reverse=True)
                final_path = str(all_files[0])

        if not final_path:
            for f in out_dir.glob(f"{base}.*"):
                try: f.unlink()
                except: pass
            raise FileNotFoundError("Downloaded file missing")

    f = Path(final_path)
    size = f.stat().st_size
    if size < 10 * 1024:
        try:
            head = f.read_bytes()[:200].lower()
            if b"<html" in head or b"<!doctype" in head:
                f.unlink()
                raise ValueError("HTML page — ye video nahi hai")
        except Exception:
            pass
        try: f.unlink()
        except: pass
        raise ValueError(f"File bahut chhota ({size} bytes)")

    ext = f.suffix.lower()
    if ext not in MEDIA_EXTS and ext != "":
        try:
            head = f.read_bytes()[:16]
            if head[4:8] == b"ftyp":
                new = f.with_suffix(".mp4"); f.rename(new); f = new
            elif head[:4] == b"%PDF":
                new = f.with_suffix(".pdf"); f.rename(new); f = new
        except Exception:
            pass

    log.info(f"✅ Final: {f.name} ({size/1024/1024:.1f} MB)")
    return f

# ───────── UPLOAD PROGRESS ─────────
class ProgressFile:
    def __init__(self, path, info):
        self._f = open(path, "rb")
        self._info = info
        self._total = os.path.getsize(path)
        self._done = 0; self._last_t = time.time(); self._last_b = 0
        info["total"], info["done"] = self._total, 0

    def read(self, size=-1):
        chunk = self._f.read(size)
        self._done += len(chunk); self._info["done"] = self._done
        t = time.time(); dt = t - self._last_t
        if dt >= 0.5:
            self._info["speed"] = (self._done - self._last_b) / dt
            self._last_t, self._last_b = t, self._done
        return chunk

    def __len__(self): return self._total
    def seek(self, o, w=0): return self._f.seek(o, w)
    def tell(self): return self._f.tell()
    def close(self): return self._f.close()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

# ═══════════════════════════════════════════════════════════
#  🎯 MAIN PROCESSOR
# ═══════════════════════════════════════════════════════════
async def process_items(update, ctx, items, batch_label=""):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    loop = asyncio.get_event_loop()
    total = len(items)
    mode = "4GB" if USE_LOCAL_API else "50MB"

    u = update.effective_user
    downloaded_by = u.first_name or u.username or str(user_id)
    if u.last_name: downloaded_by += f" {u.last_name}"

    STOP_FLAGS[user_id] = False
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel All Downloads",
                              callback_data=f"cancel:{user_id}")]
    ])

    status = await ctx.bot.send_message(
        chat_id,
        f"🎬 *Sequential Mode ({mode})*\n"
        f"Batch: {batch_label or 'Direct'}\n"
        f"Total: {total} item(s)\n\n"
        f"`/stop` ya button se rok sakte ho.",
        parse_mode="Markdown", reply_markup=cancel_kb)

    ok = 0; failed = []; stopped = False

    for idx, item in enumerate(items, 1):
        if is_stopped(user_id):
            stopped = True; break
        name = item["name"][:100]
        ftype = item["type"]
        header = f"📦 Item {idx}/{total}\n🎬 {name}\n🔖 {ftype}"

        await safe_edit(ctx.bot, chat_id, status.message_id,
                        f"{header}\n\n🔐 Step 1/3 — Verifying...", cancel_kb)
        if is_stopped(user_id):
            stopped = True; break
        try:
            final_url = await loop.run_in_executor(
                None, resolve_url, item["url"], user_id)
        except Exception as e:
            failed.append((idx, name, "verify", f"url: {str(e)[:120]}"))
            continue
        if is_stopped(user_id):
            stopped = True; break

        base = f"{user_id}_{idx}"
        dl_info = {"done": 0, "total": 0, "speed": 0}
        dl_task = loop.run_in_executor(
            None, download_file, final_url, DOWNLOAD_DIR, base, dl_info, user_id)

        while not dl_task.done():
            await asyncio.wait({dl_task}, timeout=3)
            if is_stopped(user_id):
                stopped = True; break
            txt = build_fancy_progress("Downloading", dl_info["done"],
                                       dl_info["total"], dl_info["speed"], header)
            await safe_edit(ctx.bot, chat_id, status.message_id, txt, cancel_kb)

        if stopped:
            async def _cleanup_dl(t=dl_task):
                try:
                    p = await t
                    try: os.remove(p)
                    except: pass
                except: pass
            asyncio.create_task(_cleanup_dl())
            break

        try:
            out_path = await dl_task
        except Exception as e:
            failed.append((idx, name, "download", str(e)[:120])); continue

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            failed.append((idx, name, "download",
                           f"{size_mb:.0f}MB > {MAX_UPLOAD_MB}MB"))
            try: os.remove(out_path)
            except: pass
            continue
        if is_stopped(user_id):
            try: os.remove(out_path)
            except: pass
            stopped = True; break

        thumb_path = None
        media_info = {"duration": 0.0, "width": 0, "height": 0}
        is_video = out_path.suffix.lower() in (
            ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts")
        if is_video:
            t_path = THUMB_DIR / f"{base}.jpg"
            ok_thumb = await loop.run_in_executor(
                None, generate_thumbnail, out_path, t_path)
            if ok_thumb: thumb_path = t_path
            media_info = await loop.run_in_executor(
                None, get_media_info, out_path)

        cap = get_user_caption(user_id)
        ext = out_path.suffix.lower().lstrip(".")
        name_clean = name
        if name_clean.lower().endswith(f".{ext.lower()}"):
            name_clean = name_clean[:-(len(ext) + 1)]
        display_name = (f"{name_clean[:60]}.{ext}"
                        if ext else f"{name_clean[:60]}")

        if cap["enabled"]:
            values = {
                "file_name": name_clean,
                "file_size": fmt_size(os.path.getsize(out_path)),
                "file_extension": ext or "file",
                "file_duration": fmt_duration(media_info["duration"]),
                "file_url": item["url"], "file_index": idx,
                "batch_name": batch_label or "Direct",
                "downloaded_by": downloaded_by,
            }
            caption = render_caption(cap["template"], values)[:1024]
        else:
            caption = None

        up_info = {"done": 0, "total": os.path.getsize(out_path), "speed": 0}
        wrapper = ProgressFile(str(out_path), up_info)
        thumb_fh = open(thumb_path, "rb") if thumb_path else None
        try:
            if is_video:
                coro = ctx.bot.send_video(
                    chat_id=chat_id, video=wrapper, filename=display_name,
                    caption=caption, thumbnail=thumb_fh,
                    duration=(int(media_info["duration"])
                              if media_info["duration"] else 0),
                    width=media_info["width"] or None,
                    height=media_info["height"] or None,
                    supports_streaming=True,
                    read_timeout=7200, write_timeout=7200)
            else:
                coro = ctx.bot.send_document(
                    chat_id=chat_id, document=wrapper, filename=display_name,
                    caption=caption, thumbnail=thumb_fh,
                    read_timeout=7200, write_timeout=7200)
        except Exception as e:
            wrapper.close()
            if thumb_fh: thumb_fh.close()
            try: os.remove(out_path)
            except: pass
            failed.append((idx, name, "upload", str(e)[:120])); continue

        send_task = asyncio.create_task(coro)
        while not send_task.done():
            await asyncio.wait({send_task}, timeout=3)
            if is_stopped(user_id):
                send_task.cancel(); stopped = True; break
            txt = build_fancy_progress("Uploading", up_info["done"],
                                       up_info["total"], up_info["speed"], header)
            await safe_edit(ctx.bot, chat_id, status.message_id, txt, cancel_kb)

        sent_msg = None
        try:
            if not stopped:
                sent_msg = await send_task
                ok += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            failed.append((idx, name, "upload", str(e)[:120]))
        finally:
            wrapper.close()
            if thumb_fh: thumb_fh.close()
            try: os.remove(out_path)
            except: pass
            if thumb_path and thumb_path.exists():
                try: thumb_path.unlink()
                except: pass

        # ─── Forward to channel + show link in bot ───
        if sent_msg:
            ch_link = await forward_to_channel(ctx, chat_id,
                                               sent_msg.message_id)
            if ch_link:
                try:
                    await ctx.bot.send_message(
                        chat_id,
                        f"📢 *Channel me bhi bhej diya!*\n"
                        f"📦 `{display_name}`\n"
                        f"🔗 [View in Channel]({ch_link})",
                        parse_mode="Markdown",
                        disable_web_page_preview=True)
                except Exception:
                    pass

        if stopped: break

    if stopped:
        final = (f"🛑 *STOPPED*\n\n✔️ Uploaded: {ok}/{total}\n"
                 f"❌ Failed: {len(failed)}")
    else:
        final = (f"✅ *DONE ({mode})*\n\n✔️ Uploaded: {ok}/{total}\n"
                 f"❌ Failed: {len(failed)}/{total}")
    if failed:
        final += "\n\n*Failed Items:*\n"
        for idx, n, stage, err in failed[:25]:
            final += f"• #{idx} `{n[:35]}` → {stage}: {err[:60]}\n"
        if len(failed) > 25:
            final += f"\n_...aur {len(failed)-25} fail_"
    await safe_edit(ctx.bot, chat_id, status.message_id, final)
    STOP_FLAGS.pop(user_id, None)


async def start_batch(update, ctx, items, batch_label=""):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        ok_p, _ = is_premium(user_id)
        if not ok_p:
            await update.message.reply_text(
                f"🚫 *Access Denied*\n\nAapki ID: `{user_id}`",
                parse_mode="Markdown")
            return
    if user_id in CURRENT_TASK and not CURRENT_TASK[user_id].done():
        await update.message.reply_text("⚠️ Ek batch chal rahi. /stop bhejo.")
        return
    task = asyncio.create_task(process_items(update, ctx, items, batch_label))
    CURRENT_TASK[user_id] = task

    def _done(t):
        if CURRENT_TASK.get(user_id) is t: CURRENT_TASK.pop(user_id, None)
        STOP_FLAGS.pop(user_id, None)

    task.add_done_callback(_done)

# ═══════════════════════════════════════════════════════════
#  🍪 COOKIES COMMANDS
# ═══════════════════════════════════════════════════════════
async def setcookies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return

    doc = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
    elif update.message.document:
        doc = update.message.document

    if not doc:
        await update.message.reply_text(
            "📄 *Cookies Setup*\n\n"
            "1️⃣ Browser pe YouTube/Instagram login karo\n"
            "2️⃣ Chrome extension *'Get cookies.txt LOCALLY'* install karo\n"
            "3️⃣ Export karo → `cookies.txt`\n"
            "4️⃣ Yahan bhejo:\n"
            "   `/setcookies` bhejo aur reply me file attach karo\n"
            "   YA seedha `cookies.txt` file bhejo",
            parse_mode="Markdown")
        return

    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("❌ .txt file bhejo (cookies.txt)")
        return

    msg = await update.message.reply_text("📥 Cookies save kar raha...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()
        content = bytes(data)
        if len(content) < 50:
            await msg.edit_text("❌ File bahut chhoti — galat cookies.txt")
            return
        if save_cookies_to_db(content, doc.file_name or "cookies.txt"):
            COOKIES_FILE.write_bytes(content)
            await msg.edit_text(
                f"✅ *Cookies Saved!*\n\n"
                f"📁 `{doc.file_name}`\n"
                f"💾 {len(content)} bytes\n\n"
                f"Ab YouTube, Instagram sab download hoga.",
                parse_mode="Markdown")
        else:
            await msg.edit_text("❌ MongoDB save fail")
    except Exception as e:
        await msg.edit_text(f"❌ Cookies save fail: {str(e)[:200]}")


async def getcookies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    data = load_cookies_from_db()
    if not data:
        await update.message.reply_text("📭 Koi cookies nahi."); return
    content = data.get("content")
    updated = data.get("updated_at")
    size = data.get("size", len(content) if content else 0)
    when = updated.strftime("%d %b %Y, %I:%M %p") if updated else "?"
    await update.message.reply_text(
        f"🍪 *Cookies Status*\n\n"
        f"📁 `{data.get('filename', 'cookies.txt')}`\n"
        f"💾 {size} bytes\n"
        f"⏰ Updated: {when}",
        parse_mode="Markdown")


async def delcookies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    if delete_cookies_from_db():
        try: COOKIES_FILE.unlink()
        except: pass
        await update.message.reply_text("✅ Cookies deleted.")
    else:
        await update.message.reply_text("ℹ️ Koi cookies nahi thi.")


async def handle_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_duplicate(update.update_id):
        log.warning(f"🚫 Duplicate doc update_id={update.update_id}")
        return

    uid = update.effective_user.id
    doc = update.message.document
    fname = (doc.file_name or "").lower()
    caption = (update.message.caption or "").lower()

    if fname == "cookies.txt" or "cookies" in fname or "/setcookies" in caption:
        if is_owner(uid):
            await setcookies_cmd(update, ctx)
            return

    if WAITING_TXT.get(uid):
        WAITING_TXT.pop(uid, None)
        await handle_txt_doc(update, ctx, doc)


async def handle_txt_doc(update, ctx, doc):
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("❌ .txt file bhejo"); return
    msg = await update.message.reply_text("📖 TXT padh raha hoon...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()
        items = parse_txt(data.decode("utf-8", errors="ignore"))
    except Exception as e:
        await msg.edit_text(f"❌ TXT error: {str(e)[:150]}"); return
    if not items:
        await msg.edit_text("❌ Koi valid item nahi."); return
    batch_name = (doc.file_name or "TXT")[:60]
    await msg.edit_text(f"✅ {len(items)} items. Start...")
    await start_batch(update, ctx, items, batch_label=batch_name)

# ═══════════════════════════════════════════════════════════
#  🎨 CAPTION
# ═══════════════════════════════════════════════════════════
def build_caption_menu(uid):
    cap = get_user_caption(uid)
    status = "Enabled" if cap["enabled"] else "Disabled"
    return (
        "Set Caption\n\n➤ Available Variables 📌\n\n"
        "🎙 Name : {file_name}\n📦 Size : {file_size}\n"
        "⚙️ Extension : {file_extension}\n⏱ Duration : {file_duration}\n"
        "🔗 Link : {file_url}\n🔢 Index : {file_index}\n"
        "📚 Batch Name : {batch_name}\n👤 Downloaded By : {downloaded_by}\n\n"
        "═══════════════════════\n\n➤ Current:\n"
        f"{cap['template']}\n\n═══════════════════════════\n\n➤ Default:\n"
        f"{DEFAULT_CAPTION}\n\n➤ Status: {status}"
    )


def caption_keyboard(uid):
    cap = get_user_caption(uid)
    t = "🔴 DISABLE" if cap["enabled"] else "🟢 ENABLE"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t, callback_data="cap:toggle")],
        [InlineKeyboardButton("✏️ UPDATE CAPTION", callback_data="cap:update")],
        [InlineKeyboardButton("↺ RESET DEFAULT", callback_data="cap:reset")],
        [InlineKeyboardButton("🔙 BACK", callback_data="menu:main")],
    ])


async def caption_cmd(update, ctx):
    uid = update.effective_user.id
    if not is_owner(uid):
        ok_p, _ = is_premium(uid)
        if not ok_p:
            await update.message.reply_text(
                f"🚫 Premium chahiye. ID: `{uid}`", parse_mode="Markdown")
            return
    await update.message.reply_text(build_caption_menu(uid),
                                    reply_markup=caption_keyboard(uid))


async def caption_callback(update, ctx):
    q = update.callback_query; uid = q.from_user.id
    action = q.data.split(":", 1)[1]
    if action == "toggle":
        cap = get_user_caption(uid)
        set_caption_enabled(uid, not cap["enabled"])
        await q.answer("✅")
        await q.edit_message_text(build_caption_menu(uid),
                                  reply_markup=caption_keyboard(uid))
    elif action == "update":
        WAITING_CAPTION[uid] = True
        await q.answer("Send template")
        await q.edit_message_text(
            "✏️ *Caption Update*\n\nVariables: `{file_name}` `{file_size}` "
            "`{file_extension}` `{file_duration}` `{file_url}` `{file_index}` "
            "`{batch_name}` `{downloaded_by}`\n\n/cancel se rok do.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 BACK", callback_data="menu:main")]]))
    elif action == "reset":
        reset_caption(uid); await q.answer("✅")
        await q.edit_message_text(build_caption_menu(uid),
                                  reply_markup=caption_keyboard(uid))


async def cancel_caption_cmd(update, ctx):
    uid = update.effective_user.id
    cleared = False
    for d in (WAITING_CAPTION, WAITING_GITHUB, WAITING_GITHUB_PROFILE,
              WAITING_GITHUB_SEARCH, WAITING_GITHUB_USER, WAITING_GITHUB_FILE,
              WAITING_GITHUB_CODE, WAITING_PREMIUM_ADD, WAITING_PREMIUM_REMOVE,
              WAITING_URL, WAITING_TXT):
        if d.get(uid):
            d.pop(uid, None); cleared = True
    if cleared:
        await update.message.reply_text("❌ Cancelled.")
    else:
        await update.message.reply_text("ℹ️ Kuch cancel nahi.")

# ═══════════════════════════════════════════════════════════
#  👑 OWNER COMMANDS (Premium)
# ═══════════════════════════════════════════════════════════
async def add_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/add <user_id> <days>`",
                                        parse_mode="Markdown"); return
    try:
        target = int(ctx.args[0]); days = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Numbers hone chahiye."); return
    if days <= 0:
        await update.message.reply_text("❌ Days > 0."); return
    exp_str = add_premium(target, days, update.effective_user.id)
    await update.message.reply_text(
        f"✅ *Premium Added*\n\n👤 `{target}`\n📅 +{days} days\n⏰ {exp_str}",
        parse_mode="Markdown")
    try:
        await ctx.bot.send_message(
            target,
            f"🎉 *Premium Activated!*\n\n+{days} days\nExpires: {exp_str}",
            parse_mode="Markdown")
    except: pass


async def remove_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    if not ctx.args:
        await update.message.reply_text("Usage: /remove <user_id>"); return
    try:
        target = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("❌ user_id number."); return
    if remove_premium(target):
        await update.message.reply_text(f"✅ `{target}` removed.",
                                        parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ `{target}` nahi tha.",
                                        parse_mode="Markdown")


async def list_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    users = list_premium_users()
    if not users:
        await update.message.reply_text("📭 Koi user nahi."); return
    now = now_utc(); lines = ["👥 Premium Users\n"]; a = e = 0
    for u in users:
        exp = u.get("expires")
        if not exp: continue
        if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            lines.append(f"✅ `{u['_id']}` → {(exp-now).days} din"); a += 1
        else:
            lines.append(f"❌ `{u['_id']}` → expire"); e += 1
    lines.append(f"\nTotal: {len(users)} | ✅ {a} | ❌ {e}")
    await update.message.reply_text("\n".join(lines)[:4000],
                                    parse_mode="Markdown")


async def myid_cmd(update, ctx):
    await update.message.reply_text(f"🆔 `{update.effective_user.id}`",
                                    parse_mode="Markdown")


async def premium_cmd(update, ctx):
    uid = update.effective_user.id
    if is_owner(uid):
        await update.message.reply_text("👑 Owner — unlimited."); return
    ok_p, days = is_premium(uid)
    if ok_p:
        await update.message.reply_text(f"✅ Active — {days} din.")
    else:
        await update.message.reply_text(f"🚫 Premium nahi.\nID: `{uid}`",
                                        parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════
#  🎛️ INLINE MENUS
# ═══════════════════════════════════════════════════════════
def main_menu_kb(is_owner_user=False):
    rows = [
        [InlineKeyboardButton("📤 Upload TXT", callback_data="menu:txt"),
         InlineKeyboardButton("🔗 Upload URL", callback_data="menu:url")],
        [InlineKeyboardButton("🐙 GitHub Menu", callback_data="menu:github"),
         InlineKeyboardButton("🎨 Caption", callback_data="menu:caption")],
        [InlineKeyboardButton("📊 My Status", callback_data="menu:status"),
         InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    if is_owner_user:
        rows.append([
            InlineKeyboardButton("👑 Premium Manager", callback_data="menu:premium"),
            InlineKeyboardButton("🍪 Cookies", callback_data="menu:cookies"),
        ])
    rows.append([InlineKeyboardButton("🛑 STOP current batch",
                                      callback_data="menu:stop")])
    return InlineKeyboardMarkup(rows)


def github_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐙 Download Profile Repos",
                              callback_data="gh:profile")],
        [InlineKeyboardButton("🔍 Search Repos", callback_data="gh:search"),
         InlineKeyboardButton("👤 User Info", callback_data="gh:user")],
        [InlineKeyboardButton("📁 Search Files", callback_data="gh:file"),
         InlineKeyboardButton("💻 Search Code", callback_data="gh:code")],
        [InlineKeyboardButton("📜 Download History", callback_data="gh:history")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])


def cookies_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status", callback_data="ck:status"),
         InlineKeyboardButton("🗑 Delete", callback_data="ck:delete")],
        [InlineKeyboardButton("📤 How to Upload", callback_data="ck:help")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])


def premium_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Premium User", callback_data="pm:add")],
        [InlineKeyboardButton("➖ Remove Premium User",
                              callback_data="pm:remove")],
        [InlineKeyboardButton("📋 List Premium Users", callback_data="pm:list")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])


def back_kb(target="menu:main", label="🔙 Back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])


# ═══════════════════════════════════════════════════════════
#  🎛️ MENU CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════
async def menu_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "menu:main":
        await q.edit_message_text(
            "🏠 *Main Menu*\n\nKya karna hai? Neeche button choose karo 👇",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(is_owner(uid)))
        return

    if data == "menu:github":
        await q.edit_message_text(
            "🐙 *GitHub Menu*\n\nKya karna hai? 👇",
            parse_mode="Markdown",
            reply_markup=github_menu_kb())
        return

    if data == "menu:caption":
        await q.edit_message_text(
            build_caption_menu(uid),
            reply_markup=caption_keyboard(uid))
        return

    if data == "menu:cookies":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.", reply_markup=back_kb())
            return
        await q.edit_message_text(
            "🍪 *Cookies Menu*\n\nManage YouTube/Instagram cookies 👇",
            parse_mode="Markdown",
            reply_markup=cookies_menu_kb())
        return

    if data == "menu:premium":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.", reply_markup=back_kb())
            return
        await q.edit_message_text(
            "👑 *Premium Manager*\n\nChoose action 👇",
            parse_mode="Markdown",
            reply_markup=premium_menu_kb())
        return

    if data == "menu:txt":
        if not is_owner(uid):
            ok_p, _ = is_premium(uid)
            if not ok_p:
                await q.edit_message_text("🚫 Premium chahiye.",
                                          reply_markup=back_kb())
                return
        WAITING_TXT[uid] = True
        await q.edit_message_text(
            "📄 *TXT Batch Upload*\n\n"
            "Ab .txt file bhejo (jisme URLs hain).\n\n"
            "`/cancel` se rok do.",
            parse_mode="Markdown",
            reply_markup=back_kb())
        return

    if data == "menu:url":
        if not is_owner(uid):
            ok_p, _ = is_premium(uid)
            if not ok_p:
                await q.edit_message_text("🚫 Premium chahiye.",
                                          reply_markup=back_kb())
                return
        WAITING_URL[uid] = True
        await q.edit_message_text(
            "🔗 *Direct URL Upload*\n\n"
            "Ab direct video/download URL bhejo.\n"
            "Multiple URLs bhi bhej sakte ho (new line se).\n\n"
            "`/cancel` se rok do.",
            parse_mode="Markdown",
            reply_markup=back_kb())
        return

    if data == "menu:status":
        mode = "🚀 4GB" if USE_LOCAL_API else "⚠️ 50MB"
        db = "✅" if MONGO_OK else "❌"
        ck = ("✅" if (COOKIES_FILE.exists()
                      and COOKIES_FILE.stat().st_size > 0) else "❌")
        if is_owner(uid):
            access = "👑 Owner"
        else:
            ok_p, days = is_premium(uid)
            access = f"✅ Premium ({days}d)" if ok_p else "🚫 No Premium"
        await q.edit_message_text(
            f"📊 *Your Status*\n\n"
            f"🆔 ID: `{uid}`\n"
            f"🎫 Access: {access}\n"
            f"🔧 Mode: {mode}\n"
            f"📦 Max Upload: {MAX_UPLOAD_MB} MB\n"
            f"🗄 DB: {db} | 🍪 Cookies: {ck}\n"
            f"📢 Channel: {'✅' if CHANNEL_ID else '❌'}",
            parse_mode="Markdown",
            reply_markup=back_kb())
        return

    if data == "menu:help":
        await q.edit_message_text(
            "❓ *Help*\n\n"
            "• 📤 *Upload TXT* — TXT file jisme URLs\n"
            "• 🔗 *Upload URL* — Direct URL(s)\n"
            "• 🐙 *GitHub* — Profile download, search, etc.\n"
            "• 🎨 *Caption* — Apna custom caption\n"
            "• 📊 *Status* — Apna status\n"
            "• 🛑 *Stop* — Current batch rok do\n\n"
            "*All uploads automatically forwarded to channel!*\n"
            "📢 Link bot me bhi show hoga.",
            parse_mode="Markdown",
            reply_markup=back_kb())
        return

    if data == "menu:stop":
        STOP_FLAGS[uid] = True
        task = CURRENT_TASK.get(uid)
        if task and not task.done():
            await q.edit_message_text("🛑 *Stop requested!*",
                                      parse_mode="Markdown",
                                      reply_markup=back_kb())
        else:
            await q.edit_message_text("ℹ️ Koi active batch nahi.",
                                      reply_markup=back_kb())
        return

    if data == "gh:profile":
        WAITING_GITHUB_PROFILE[uid] = True
        await q.edit_message_text(
            "🐙 *Download Profile Repos*\n\n"
            "GitHub username ya profile URL bhejo.\n"
            "Example: `https://github.com/username`\n"
            "Or simply: `username`\n\n"
            "`/cancel` se rok do.",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return

    if data == "gh:search":
        WAITING_GITHUB_SEARCH[uid] = True
        await q.edit_message_text(
            "🔍 *Search Repositories*\n\nKeyword bhejo.\nExample: `python bot`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return

    if data == "gh:user":
        WAITING_GITHUB_USER[uid] = True
        await q.edit_message_text(
            "👤 *GitHub User Info*\n\nUsername bhejo.\nExample: `torvalds`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return

    if data == "gh:file":
        WAITING_GITHUB_FILE[uid] = True
        await q.edit_message_text(
            "📁 *Search Files on GitHub*\n\nFilename bhejo.\n"
            "Example: `bot.py`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return

    if data == "gh:code":
        WAITING_GITHUB_CODE[uid] = True
        await q.edit_message_text(
            "💻 *Search Code on GitHub*\n\nKeyword bhejo.\n"
            "Example: `telegram bot python`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return

    if data == "gh:history":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:github",
                                                           "🔙 GitHub Menu"))
            return
        hist = load_history()
        if not hist:
            await q.edit_message_text("📭 History empty.",
                                      reply_markup=back_kb("menu:github",
                                                           "🔙 GitHub Menu"))
            return
        lines = [f"📜 *GitHub History* ({len(hist)} items)\n"]
        for i, name in enumerate(sorted(hist)[:50], 1):
            lines.append(f"{i}. `{name}`")
        if len(hist) > 50:
            lines.append(f"\n_...aur {len(hist)-50} items_")
        lines.append("\n\nUse `/ghhistory clear` to wipe.")
        text = "\n".join(lines)[:4000]
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_kb("menu:github",
                                                       "🔙 GitHub Menu"))
        return

    if data == "ck:status":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:cookies"))
            return
        ck_data = load_cookies_from_db()
        if not ck_data:
            await q.edit_message_text("📭 Koi cookies nahi.",
                                      reply_markup=back_kb("menu:cookies"))
            return
        updated = ck_data.get("updated_at")
        size = ck_data.get("size", 0)
        when = updated.strftime("%d %b %Y, %I:%M %p") if updated else "?"
        await q.edit_message_text(
            f"🍪 *Cookies Status*\n\n"
            f"📁 `{ck_data.get('filename', 'cookies.txt')}`\n"
            f"💾 {size} bytes\n"
            f"⏰ Updated: {when}",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:cookies"))
        return

    if data == "ck:delete":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:cookies"))
            return
        if delete_cookies_from_db():
            try: COOKIES_FILE.unlink()
            except: pass
            await q.edit_message_text("✅ Cookies deleted.",
                                      reply_markup=back_kb("menu:cookies"))
        else:
            await q.edit_message_text("ℹ️ Koi cookies nahi thi.",
                                      reply_markup=back_kb("menu:cookies"))
        return

    if data == "ck:help":
        await q.edit_message_text(
            "🍪 *How to Upload Cookies*\n\n"
            "1️⃣ Chrome me *'Get cookies.txt LOCALLY'* extension install karo\n"
            "2️⃣ YouTube/Instagram pe login karo\n"
            "3️⃣ Extension se export → `cookies.txt`\n"
            "4️⃣ Yahan `/setcookies` bhejo aur reply me file attach karo\n"
            "   YA seedha `cookies.txt` file send kar do",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:cookies"))
        return

    if data == "pm:add":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:premium"))
            return
        WAITING_PREMIUM_ADD[uid] = True
        await q.edit_message_text(
            "➕ *Add Premium*\n\n"
            "Format: `<user_id> <days>`\n"
            "Example: `123456789 30`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        return

    if data == "pm:remove":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:premium"))
            return
        WAITING_PREMIUM_REMOVE[uid] = True
        await q.edit_message_text(
            "➖ *Remove Premium*\n\nUser ID bhejo.\nExample: `123456789`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        return

    if data == "pm:list":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:premium"))
            return
        users = list_premium_users()
        if not users:
            await q.edit_message_text("📭 Koi user nahi.",
                                      reply_markup=back_kb("menu:premium"))
            return
        now = now_utc(); lines = ["👥 *Premium Users*\n"]; a = e = 0
        for u in users:
            exp = u.get("expires")
            if not exp: continue
            if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
            if exp > now:
                lines.append(f"✅ `{u['_id']}` → {(exp-now).days}d"); a += 1
            else:
                lines.append(f"❌ `{u['_id']}` → expired"); e += 1
        lines.append(f"\nTotal: {len(users)} | ✅ {a} | ❌ {e}")
        await q.edit_message_text("\n".join(lines)[:4000],
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:premium"))
        return


# ═══════════════════════════════════════════════════════════
#  🚀 /start, /help
# ═══════════════════════════════════════════════════════════
async def start(update, ctx):
    if is_duplicate(update.update_id):
        log.warning(f"🚫 Duplicate start update_id={update.update_id}")
        return

    uid = update.effective_user.id
    mode = "🚀 4GB" if USE_LOCAL_API else "⚠️ 50MB"
    db = "✅" if MONGO_OK else "❌"
    ck = ("✅" if (COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0)
          else "❌")
    if is_owner(uid):
        access = "👑 Owner"
    else:
        ok_p, days = is_premium(uid)
        access = f"✅ Premium ({days}d)" if ok_p else "🚫 No Premium"
    ch = "✅" if CHANNEL_ID else "❌"

    await update.message.reply_text(
        f"👋 *Welcome to Course Uploader Bot!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔧 Mode: {mode}\n"
        f"📦 Max Upload: {MAX_UPLOAD_MB} MB\n"
        f"🎫 Access: {access}\n"
        f"🗄 DB: {db} | 🍪 Cookies: {ck} | 📢 Channel: {ch}\n\n"
        f"📌 *Sab uploads channel me bhi forward honge!*\n"
        f"🔗 Link turant bot me milega.\n\n"
        f"👉 Neeche menu se choose karo 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(is_owner(uid)))


async def help_cmd(update, ctx):
    uid = update.effective_user.id
    await update.message.reply_text(
        "❓ *Help Menu*\n\n"
        "📤 `/txt` — TXT file batch upload\n"
        "🔗 URL direct bhejo — auto download\n"
        "🐙 `/github` — GitHub profile → repos\n"
        "🔍 `/search <kw>` — Search repos\n"
        "👤 `/user <name>` — GitHub user info\n"
        "📁 `/file <name>` — Search files\n"
        "💻 `/code <kw>` — Search code\n"
        "📜 `/ghhistory` — History view\n"
        "🎨 `/caption` — Custom caption\n"
        "🛑 `/stop` — Stop current batch\n"
        "👤 `/myid` — Get your ID\n"
        "🎫 `/premium` — Premium status\n\n"
        "*Owner commands:*\n"
        "👑 `/add <id> <days>`\n"
        "➖ `/remove <id>`\n"
        "📋 `/list`\n"
        "🍪 `/setcookies`, `/getcookies`, `/delcookies`\n"
        "🔑 `/replacetoken`, `/tokenstatus`\n\n"
        "📢 *Every file forwarded to channel + link shown!*",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(is_owner(uid)))


async def stop_cmd(update, ctx):
    uid = update.effective_user.id
    STOP_FLAGS[uid] = True
    task = CURRENT_TASK.get(uid)
    if task and not task.done():
        await update.message.reply_text("🛑 Stop requested!")
    else:
        await update.message.reply_text("ℹ️ Koi active batch nahi.")


async def cancel_cb(update, ctx):
    q = update.callback_query; await q.answer("Cancelling...")
    try:
        _, uid_str = q.data.split(":"); uid = int(uid_str)
    except Exception:
        return
    STOP_FLAGS[uid] = True
    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass


async def txt_cmd(update, ctx):
    uid = update.effective_user.id
    if not is_owner(uid):
        ok_p, _ = is_premium(uid)
        if not ok_p:
            await update.message.reply_text("🚫 Premium chahiye."); return
    if update.message.reply_to_message and update.message.reply_to_message.document:
        await handle_txt_doc(update, ctx,
                             update.message.reply_to_message.document)
        return
    WAITING_TXT[uid] = True
    await update.message.reply_text("📄 Ab .txt file bhejo.")


async def github_cmd(update, ctx):
    uid = update.effective_user.id
    if not is_owner(uid):
        ok_p, _ = is_premium(uid)
        if not ok_p:
            await update.message.reply_text("🚫 Premium chahiye."); return
    if ctx.args:
        url = ctx.args[0]
        if not url.startswith("http"):
            url = "https://" + url
        if is_github_profile_url(url):
            await github_show_repos(update, ctx, url)
            return
        else:
            await update.message.reply_text("❌ Valid GitHub profile URL nahi.")
            return
    await update.message.reply_text(
        "🐙 *GitHub Downloader*\n\n"
        "GitHub profile ka URL bhejo jaise:\n"
        "`https://github.com/username`\n\n"
        "Ya neeche menu use karo 👇",
        parse_mode="Markdown",
        reply_markup=github_menu_kb())


# ═══════════════════════════════════════════════════════════
#  📨 TEXT HANDLER — all flows
# ═══════════════════════════════════════════════════════════
async def handle_text(update, ctx):
    if is_duplicate(update.update_id):
        log.warning(f"🚫 Duplicate text update_id={update.update_id}")
        return

    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"): return

    if WAITING_CAPTION.get(uid):
        WAITING_CAPTION.pop(uid, None)
        if len(text) > 900:
            await update.message.reply_text("❌ Max 900 chars."); return
        set_caption_template(uid, text)
        await update.message.reply_text("✅ Caption updated!")
        await update.message.reply_text(build_caption_menu(uid),
                                        reply_markup=caption_keyboard(uid))
        return

    if WAITING_GITHUB.get(uid):
        handled = await github_process_range(update, ctx, uid, text)
        if handled:
            return

    if WAITING_GITHUB_PROFILE.get(uid):
        WAITING_GITHUB_PROFILE.pop(uid, None)
        url = text if text.startswith("http") else f"https://github.com/{text}"
        if is_github_profile_url(url):
            await github_show_repos(update, ctx, url)
            return
        else:
            await update.message.reply_text(
                "❌ Valid GitHub username/profile nahi mila.")
            return

    if WAITING_GITHUB_SEARCH.get(uid):
        WAITING_GITHUB_SEARCH.pop(uid, None)
        ctx.args = text.split()
        await github_search_cmd(update, ctx)
        return

    if WAITING_GITHUB_USER.get(uid):
        WAITING_GITHUB_USER.pop(uid, None)
        ctx.args = [text.split()[0]]
        await github_user_cmd(update, ctx)
        return

    if WAITING_GITHUB_FILE.get(uid):
        WAITING_GITHUB_FILE.pop(uid, None)
        ctx.args = text.split()
        await github_file_cmd(update, ctx)
        return

    if WAITING_GITHUB_CODE.get(uid):
        WAITING_GITHUB_CODE.pop(uid, None)
        ctx.args = text.split()
        await github_code_cmd(update, ctx)
        return

    if WAITING_PREMIUM_ADD.get(uid):
        WAITING_PREMIUM_ADD.pop(uid, None)
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Format: `<user_id> <days>`", parse_mode="Markdown")
            return
        try:
            target = int(parts[0]); days = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Numbers hone chahiye."); return
        if days <= 0:
            await update.message.reply_text("❌ Days > 0."); return
        exp_str = add_premium(target, days, uid)
        await update.message.reply_text(
            f"✅ *Premium Added*\n\n👤 `{target}`\n📅 +{days} days\n⏰ {exp_str}",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        try:
            await ctx.bot.send_message(
                target,
                f"🎉 *Premium Activated!*\n\n+{days} days\nExpires: {exp_str}",
                parse_mode="Markdown")
        except: pass
        return

    if WAITING_PREMIUM_REMOVE.get(uid):
        WAITING_PREMIUM_REMOVE.pop(uid, None)
        try:
            target = int(text.split()[0])
        except Exception:
            await update.message.reply_text("❌ user_id number."); return
        if remove_premium(target):
            await update.message.reply_text(
                f"✅ `{target}` removed.",
                parse_mode="Markdown",
                reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        else:
            await update.message.reply_text(
                f"ℹ️ `{target}` nahi tha.",
                parse_mode="Markdown",
                reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        return

    if WAITING_URL.get(uid):
        WAITING_URL.pop(uid, None)
        if not URL_RE.search(text):
            await update.message.reply_text("❌ Koi URL nahi mila.")
            return
        items = parse_txt(text)
        if not items:
            await update.message.reply_text("❌ Koi valid URL nahi mila.")
            return
        await update.message.reply_text(f"✅ {len(items)} item(s). Shuru...")
        await start_batch(update, ctx, items, batch_label="Direct URL")
        return

    if is_github_profile_url(text):
        await github_show_repos(update, ctx, text)
        return

    if not URL_RE.search(text):
        await update.message.reply_text(
            "❓ URL bhejo ya /help dekho.",
            reply_markup=main_menu_kb(is_owner(uid)))
        return
    items = parse_txt(text)
    if not items:
        await update.message.reply_text("❌ Koi valid URL nahi mila."); return
    await update.message.reply_text(f"✅ {len(items)} item(s). Shuru...")
    await start_batch(update, ctx, items, batch_label="Direct URL")


# ═══════════════════════════════════════════════════════════
#  🎯 POST INIT
# ═══════════════════════════════════════════════════════════
async def post_init(app):
    general_commands = [
        BotCommand("start", "Bot info + Menu"),
        BotCommand("help", "Help + Menu"),
        BotCommand("txt", "TXT batch upload"),
        BotCommand("github", "GitHub menu"),
        BotCommand("search", "Search GitHub repos"),
        BotCommand("user", "GitHub user info"),
        BotCommand("file", "Search files on GitHub"),
        BotCommand("code", "Search code on GitHub"),
        BotCommand("ghhistory", "GitHub download history"),
        BotCommand("stop", "Stop current batch"),
        BotCommand("cancel", "Cancel waiting state"),
        BotCommand("caption", "Custom caption setup"),
        BotCommand("premium", "Check premium status"),
        BotCommand("myid", "Get your Telegram ID"),
    ]
    owner_commands = general_commands + [
        BotCommand("add", "Add premium (owner)"),
        BotCommand("remove", "Remove premium (owner)"),
        BotCommand("list", "List premium users (owner)"),
        BotCommand("setcookies", "Upload cookies (owner)"),
        BotCommand("getcookies", "Cookies status (owner)"),
        BotCommand("delcookies", "Delete cookies (owner)"),
        BotCommand("replacetoken", "Set GitHub token (owner)"),
        BotCommand("tokenstatus", "Show GitHub token (owner)"),
    ]
    try:
        await app.bot.set_my_commands(general_commands,
                                      scope=BotCommandScopeDefault())
    except Exception as e:
        log.warning(f"⚠️ Default menu fail: {e}")
    try:
        await app.bot.set_my_commands(
            owner_commands,
            scope=BotCommandScopeChat(chat_id=int(OWNER_ID)))
    except Exception as e:
        log.warning(f"⚠️ Owner menu fail: {e}")


# ═══════════════════════════════════════════════════════════
#  🚀 MAIN
# ═══════════════════════════════════════════════════════════
def build_application():
    builder = (Application.builder()
               .token(BOT_TOKEN)
               .concurrent_updates(16)
               .post_init(post_init))
    if USE_LOCAL_API:
        builder = (builder.base_url(LOCAL_API_BASE)
                          .base_file_url(LOCAL_FILE_BASE)
                          .local_mode(True))
    app = builder.build()

    # ── Core ──
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("txt", txt_cmd))
    app.add_handler(CommandHandler("caption", caption_cmd))
    app.add_handler(CommandHandler("cancel", cancel_caption_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))

    # ── GitHub ──
    app.add_handler(CommandHandler("github", github_cmd))
    app.add_handler(CommandHandler("search", github_search_cmd))
    app.add_handler(CommandHandler("user", github_user_cmd))
    app.add_handler(CommandHandler("file", github_file_cmd))
    app.add_handler(CommandHandler("code", github_code_cmd))
    app.add_handler(CommandHandler("ghhistory", github_history_cmd))
    app.add_handler(CommandHandler("replacetoken", github_replacetoken_cmd))
    app.add_handler(CommandHandler("tokenstatus", github_tokenstatus_cmd))

    # ── Owner ──
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("setcookies", setcookies_cmd))
    app.add_handler(CommandHandler("getcookies", getcookies_cmd))
    app.add_handler(CommandHandler("delcookies", delcookies_cmd))

    # ── Callbacks ──
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^gh:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^ck:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^pm:"))
    app.add_handler(CallbackQueryHandler(caption_callback, pattern=r"^cap:"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^cancel:"))

    # ── Messages ──
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


def run_bot():
    while True:
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            app = build_application()

            log.info(
                f"🤖 Bot starting... Mode: {'4GB' if USE_LOCAL_API else '50MB'} | "
                f"MongoDB: {'✅' if MONGO_OK else '❌'} | "
                f"Cookies: {'✅' if COOKIES_FILE.exists() else '❌'} | "
                f"Channel: {CHANNEL_ID} | Volume: {VOLUME_DIR}"
            )

            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False
            )
            break
        except KeyboardInterrupt:
            log.info("👋 KeyboardInterrupt — exiting.")
            break
        except Exception as e:
            err = str(e)
            log.error(f"❌ Crash: {err}")
            if "Logged out" in err or "Unauthorized" in err:
                log.error("⏳ Token issue — 60 sec wait, retry...")
                time.sleep(60)
            elif "Conflict" in err or "terminated by other" in err:
                log.error("⏳ Another instance running — 30 sec wait...")
                time.sleep(30)
            else:
                log.error("⏳ 10 sec wait, retry...")
                time.sleep(10)


if __name__ == "__main__":
    run_bot()
