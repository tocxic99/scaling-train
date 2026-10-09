# ═══════════════════════════════════════════════════════════
#  ⚙️  CONFIG — Defaults (Railway env vars)
# ═══════════════════════════════════════════════════════════
import os

DEFAULT_BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DEFAULT_API_ID    = os.environ.get("API_ID", "")
DEFAULT_API_HASH  = os.environ.get("API_HASH", "")
OWNER_ID          = int(os.environ.get("OWNER_ID", ""))

MONGO_URI = os.environ.get(
    "MONGO_URI",
    "")
DB_NAME   = os.environ.get("DB_NAME", "UPLOADER_BOT")

DEFAULT_COURSE_ID       = os.environ.get("COURSE_ID", "41")
DEFAULT_FALLBACK_USERID = os.environ.get("FALLBACK_USERID", "464995")
DEFAULT_AKAMAI_HOST     = os.environ.get("AKAMAI_HOST", "armathsapi.akamai.net.in")
DEFAULT_GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
DEFAULT_CHANNEL_ID      = int(os.environ.get("CHANNEL_ID", "-1004350191024")) or None
DEFAULT_MAX_UPLOAD_MB   = int(os.environ.get("MAX_UPLOAD_MB", ""))

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
#  📂 VOLUME DIRECTORIES
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
    cid = str(get_channel_id() or "")
    internal = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"


# ═══════════════════════════════════════════════════════════
#  🗄️ MONGODB
# ═══════════════════════════════════════════════════════════
users_col = captions_col = cookies_col = history_col = settings_col = stats_col = allusers_col = None
MONGO_OK = False

try:
    from pymongo import MongoClient
    _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    _client.server_info()
    _db = _client[DB_NAME]
    users_col     = _db["users"]        # premium users
    captions_col  = _db["captions"]
    cookies_col   = _db["cookies"]
    history_col   = _db["github_history"]
    settings_col  = _db["settings"]
    stats_col     = _db["stats"]
    allusers_col  = _db["all_users"]    # ALL users (auto-captured)
    MONGO_OK = True
    log.info(f"✅ MongoDB connected: {DB_NAME}")
except Exception as e:
    log.error(f"❌ MongoDB fail: {e}")

# ═══════════════════════════════════════════════════════════
#  👥 AUTO USER CAPTURE
# ═══════════════════════════════════════════════════════════
def capture_user(user):
    """Auto-save every user who interacts with the bot."""
    if not MONGO_OK:
        return
    try:
        uid = str(user.id)
        now = datetime.now(timezone.utc)
        allusers_col.update_one(
            {"_id": uid},
            {
                "$set": {
                    "user_id": user.id,
                    "username": user.username or "",
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "language_code": user.language_code or "",
                    "is_premium": bool(getattr(user, "is_premium", False)),
                    "last_seen": now,
                },
                "$setOnInsert": {"first_seen": now},
                "$inc": {"interactions": 1}
            },
            upsert=True)
    except Exception as e:
        log.warning(f"capture_user fail: {e}")


def list_all_users():
    if not MONGO_OK: return []
    try:
        return list(allusers_col.find({}).sort("last_seen", -1))
    except Exception:
        return []


def count_all_users():
    if not MONGO_OK: return 0
    try:
        return allusers_col.count_documents({})
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════
#  ⚙️ DYNAMIC SETTINGS
# ═══════════════════════════════════════════════════════════
_SETTINGS_CACHE = {}
_CACHE_TTL = 30


def _cache_expired(key):
    t = _SETTINGS_CACHE.get(f"{key}__t", 0)
    return time.time() - t > _CACHE_TTL


def get_setting(key, default=None):
    if not MONGO_OK:
        return default
    if not _cache_expired(key) and key in _SETTINGS_CACHE:
        return _SETTINGS_CACHE[key]
    try:
        doc = settings_col.find_one({"_id": key})
        if doc and "value" in doc:
            val = doc["value"]
            _SETTINGS_CACHE[key] = val
            _SETTINGS_CACHE[f"{key}__t"] = time.time()
            return val
    except Exception as e:
        log.warning(f"get_setting fail {key}: {e}")
    return default


def set_setting(key, value):
    if not MONGO_OK:
        return False
    try:
        settings_col.update_one(
            {"_id": key},
            {"$set": {"value": value,
                      "updated_at": datetime.now(timezone.utc)}},
            upsert=True)
        _SETTINGS_CACHE[key] = value
        _SETTINGS_CACHE[f"{key}__t"] = time.time()
        return True
    except Exception as e:
        log.warning(f"set_setting fail {key}: {e}")
        return False


def del_setting(key):
    if not MONGO_OK:
        return False
    try:
        settings_col.delete_one({"_id": key})
        _SETTINGS_CACHE.pop(key, None)
        _SETTINGS_CACHE.pop(f"{key}__t", None)
        return True
    except Exception:
        return False


def get_api_id():        return str(get_setting("api_id", DEFAULT_API_ID))
def get_api_hash():      return str(get_setting("api_hash", DEFAULT_API_HASH))
def get_course_id():     return str(get_setting("course_id", DEFAULT_COURSE_ID))
def get_fallback_userid(): return str(get_setting("fallback_userid", DEFAULT_FALLBACK_USERID))
def get_akamai_host():   return str(get_setting("akamai_host", DEFAULT_AKAMAI_HOST))
def get_github_token():  return str(get_setting("github_token", DEFAULT_GITHUB_TOKEN))
def get_channel_id():
    val = get_setting("channel_id", DEFAULT_CHANNEL_ID)
    try:
        return int(val) if val else None
    except Exception:
        return None
def get_max_upload_mb(): return int(get_setting("max_upload_mb", DEFAULT_MAX_UPLOAD_MB))
def get_local_api_url(): return str(get_setting("local_api_url", os.environ.get("LOCAL_API_URL", "")))
def get_bot_token():     return str(get_setting("bot_token", DEFAULT_BOT_TOKEN))


def github_headers():
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Mozilla/5.0 (CourseUploaderBot)"
    }
    tk = get_github_token()
    if tk:
        h["Authorization"] = f"token {tk}"
    return h


# ═══════════════════════════════════════════════════════════
#  🔧 LOCAL BOT API SERVER
# ═══════════════════════════════════════════════════════════
LOCAL_API_PORT = int(os.environ.get("LOCAL_API_PORT", "8081"))
LOCAL_API_DIR  = os.environ.get("LOCAL_API_DIR", "/tmp/tg-bot-api")
os.makedirs(LOCAL_API_DIR, exist_ok=True)


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
            try: os.chmod(p, 0o755)
            except Exception: pass
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


def auto_logout_public_api(bot_token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/logOut",
                         timeout=10)
        if r.json().get("ok"):
            log.info("✅ Public API logout")
    except Exception as e:
        log.warning(f"⚠️ Logout fail: {e}")


def start_local_api_server():
    LOCAL_API_URL = get_local_api_url()
    if LOCAL_API_URL:
        log.info(f"🌐 External Local API: {LOCAL_API_URL}")
        return True
    binary_path = find_local_api_binary()
    if not binary_path:
        log.warning("⚠️ telegram-bot-api binary NOT found — 50MB mode.")
        return False
    log.info(f"✅ Found binary: {binary_path}")
    kill_existing_api()
    auto_logout_public_api(get_bot_token())
    try:
        subprocess.Popen(
            [binary_path, f"--api-id={get_api_id()}",
             f"--api-hash={get_api_hash()}",
             "--local", f"--http-port={LOCAL_API_PORT}",
             f"--dir={LOCAL_API_DIR}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
        try:
            r = requests.get(
                f"http://localhost:{LOCAL_API_PORT}/bot{get_bot_token()}/getMe",
                timeout=5)
            if r.status_code == 200:
                log.info("✅ Local API ready — 4GB mode ON")
                return True
        except Exception:
            pass
    except Exception as e:
        log.warning(f"⚠️ Local API start fail: {e}")
    return False


LOCAL_API_OK = start_local_api_server()
USE_LOCAL_API = LOCAL_API_OK

if LOCAL_API_OK:
    _LAU = get_local_api_url()
    if _LAU:
        LOCAL_API_BASE  = f"{_LAU}/bot"
        LOCAL_FILE_BASE = f"{_LAU}/file/bot"
    else:
        LOCAL_API_BASE  = f"http://localhost:{LOCAL_API_PORT}/bot"
        LOCAL_FILE_BASE = f"http://localhost:{LOCAL_API_PORT}/file/bot"
else:
    LOCAL_API_BASE  = "https://api.telegram.org/bot"
    LOCAL_FILE_BASE = "https://api.telegram.org/file/bot"

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
WAITING_ADMIN_SETTING  = {}
WAITING_BROADCAST      = {}
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
#  🐙 GITHUB HELPERS
# ═══════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════
#  📜 GITHUB HISTORY
# ═══════════════════════════════════════════════════════════
def load_history():
    if MONGO_OK:
        try:
            return set(d["_id"] for d in history_col.find({}, {"_id": 1}))
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
            history_col.update_one(
                {"_id": repo_name},
                {"$set": {"ts": datetime.now(timezone.utc)}}, upsert=True)
            return
        except Exception:
            pass


def clear_history():
    if MONGO_OK:
        try: history_col.delete_many({})
        except Exception: pass


# ═══════════════════════════════════════════════════════════
#  🍪 COOKIES
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
#  🗄️ PREMIUM DB
# ═══════════════════════════════════════════════════════════
def now_utc(): return datetime.now(timezone.utc)


def is_premium(user_id):
    if not MONGO_OK: return False, 0
    try:
        u = users_col.find_one({"_id": str(user_id)})
        if not u: return False, 0
        exp = u.get("expires")
        if not exp: return False, 0
        if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
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


# ═══════════════════════════════════════════════════════════
#  🎨 CAPTION DB
# ═══════════════════════════════════════════════════════════
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
    except Exception: pass


def set_caption_template(uid, template):
    if not MONGO_OK: return
    try:
        captions_col.update_one({"_id": str(uid)},
                                {"$set": {"template": template}}, upsert=True)
    except Exception: pass


def reset_caption(uid):
    if not MONGO_OK: return
    try:
        captions_col.update_one({"_id": str(uid)},
                                {"$set": {"template": DEFAULT_CAPTION}},
                                upsert=True)
    except Exception: pass


def render_caption(template, values):
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ═══════════════════════════════════════════════════════════
#  📊 STATS
# ═══════════════════════════════════════════════════════════
def bump_stat(key, amount=1):
    if not MONGO_OK: return
    try:
        stats_col.update_one(
            {"_id": key},
            {"$inc": {"value": amount},
             "$set": {"updated_at": datetime.now(timezone.utc)}},
            upsert=True)
    except Exception: pass


def get_stat(key, default=0):
    if not MONGO_OK: return default
    try:
        d = stats_col.find_one({"_id": key})
        return d.get("value", default) if d else default
    except Exception:
        return default


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
    host = get_akamai_host()
    return [
        f"https://{host}/{cid}/{vid}.mp4",
        f"https://{host}/videos/{cid}/{vid}.mp4",
        f"https://{host}/video/{cid}/{vid}.mp4",
        f"https://{host}/media/{cid}/{vid}.mp4",
        f"https://{host}/hls/{cid}/{vid}.m3u8",
    ]


def is_url_live(url, timeout=3):
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code in (200, 206)
    except Exception:
        return False


def is_stopped(user_id): return STOP_FLAGS.get(user_id, False)


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
        userid    = qs.get("userid",    [get_fallback_userid()])[0]
        course_id = qs.get("course_id", [get_course_id()])[0]
        if video_id and token:
            return find_working_url(video_id, course_id, token, userid, user_id)
    return url


# ═══════════════════════════════════════════════════════════
#  🖼️ THUMBNAIL + MEDIA INFO
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
    except Exception: pass
    return info


# ─── FORMAT HELPERS ───
def fmt_size(b):
    b = float(b or 0)
    if b < 1024: return f"{b:.0f} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def fmt_size_mib(b):
    b = float(b or 0); mib = b / (1024 * 1024)
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
    filled = int(width * pct / 100); filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)


def build_fancy_progress(phase, done, total, speed, header=""):
    pct = (done / total * 100) if total else 0.0
    bar = make_bar(pct, 20)
    eta = fmt_eta_fancy((total - done) / speed
                        if speed and total > done else None)
    lines = []
    if header: lines.append(header); lines.append("")
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
    except Exception: pass


# ─── TXT PARSER ───
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
        except Exception: pass
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
        except Exception: pass

    log.info(f"✅ Final: {f.name} ({size/1024/1024:.1f} MB)")
    return f


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
#  📢 FORWARD TO CHANNEL (SILENT)
# ═══════════════════════════════════════════════════════════
async def forward_to_channel(ctx, source_chat_id, message_id):
    """Silent forward — user ko koi notification nahi dikhega."""
    ch = get_channel_id()
    if not ch:
        return None
    try:
        fwd = await ctx.bot.forward_message(
            chat_id=ch, from_chat_id=source_chat_id,
            message_id=message_id, read_timeout=120, write_timeout=120)
        log.info(f"✅ Forwarded to channel (silent): msg_id={fwd.message_id}")
        return fwd.message_id
    except Exception as e:
        log.warning(f"⚠️ Forward fail: {e}")
        return None


async def send_channel_notice(ctx, text):
    ch = get_channel_id()
    if not ch:
        return
    try:
        await ctx.bot.send_message(ch, text, parse_mode="Markdown",
                                   disable_web_page_preview=True)
    except Exception as e:
        log.warning(f"Channel notice fail: {e}")


# ═══════════════════════════════════════════════════════════
#  🎯 MAIN UPLOAD PROCESSOR
# ═══════════════════════════════════════════════════════════
async def process_items(update, ctx, items, batch_label=""):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    loop = asyncio.get_event_loop()
    total = len(items)
    mode = "4GB" if USE_LOCAL_API else "50MB"
    max_mb = get_max_upload_mb()

    u = update.effective_user
    downloaded_by = u.first_name or u.username or str(user_id)
    if u.last_name: downloaded_by += f" {u.last_name}"

    STOP_FLAGS[user_id] = False
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel All", callback_data=f"cancel:{user_id}")]
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
        if is_stopped(user_id): stopped = True; break
        name = item["name"][:100]
        ftype = item["type"]
        header = f"📦 Item {idx}/{total}\n🎬 {name}\n🔖 {ftype}"

        await safe_edit(ctx.bot, chat_id, status.message_id,
                        f"{header}\n\n🔐 Step 1/3 — Verifying...", cancel_kb)
        if is_stopped(user_id): stopped = True; break
        try:
            final_url = await loop.run_in_executor(
                None, resolve_url, item["url"], user_id)
        except Exception as e:
            failed.append((idx, name, "verify", f"url: {str(e)[:120]}")); continue
        if is_stopped(user_id): stopped = True; break

        base = f"{user_id}_{idx}"
        dl_info = {"done": 0, "total": 0, "speed": 0}
        dl_task = loop.run_in_executor(
            None, download_file, final_url, DOWNLOAD_DIR, base, dl_info, user_id)

        while not dl_task.done():
            await asyncio.wait({dl_task}, timeout=3)
            if is_stopped(user_id): stopped = True; break
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
        if size_mb > max_mb:
            failed.append((idx, name, "download",
                           f"{size_mb:.0f}MB > {max_mb}MB"))
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
                sent_msg = await send_task; ok += 1
                bump_stat("total_uploads", 1)
        except asyncio.CancelledError: pass
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

        # ✅ SILENT forward to channel — no notification to user
        if sent_msg:
            await forward_to_channel(ctx, chat_id, sent_msg.message_id)

        if stopped: break

    if stopped:
        final = f"🛑 *STOPPED*\n\n✔️ Uploaded: {ok}/{total}\n❌ Failed: {len(failed)}"
    else:
        final = f"✅ *DONE ({mode})*\n\n✔️ Uploaded: {ok}/{total}\n❌ Failed: {len(failed)}/{total}"
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
#  🐙 GITHUB — Profile Download
# ═══════════════════════════════════════════════════════════
def fetch_all_repos(username: str):
    repos = []; page = 1; per_page = 100
    while True:
        url = (f"https://api.github.com/users/{username}/repos"
               f"?per_page={per_page}&page={page}&sort=created&direction=asc")
        try:
            r = requests.get(url, headers=github_headers(),
                             timeout=20, **get_requests_kwargs())
            if r.status_code == 404: return None, "User nahi mila"
            if r.status_code == 401:
                return None, "❌ Token invalid — /replacetoken se naya set karo"
            if r.status_code == 403 or r.status_code == 429:
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 3600))
                wait = max(0, reset - int(time.time())) + 5
                return None, f"Rate limit — {wait // 60} min baad try karo"
            if r.status_code != 200:
                return None, f"GitHub API error: {r.status_code}"
            data = r.json()
            if not data: break
            repos.extend(data)
            if len(data) < per_page: break
            page += 1
            if page > 50: break
        except Exception as e:
            return None, f"Network error: {str(e)[:100]}"
    return repos, None


def format_size_kb(kb):
    if kb < 1024: return f"{kb:.2f} KB"
    return f"{kb / 1024:.2f} MB"


def parse_range(text: str, total: int):
    text = text.strip().replace(" ", "")
    if not text: return None
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
    name = repo["name"]; owner = repo["owner"]["login"]
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
        total = int(r.headers.get("content-length", 0)); done = 0
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk: continue
                f.write(chunk); done += len(chunk)
                if progress_cb: progress_cb(done, total)
        return zip_path, None
    except Exception as e:
        return None, str(e)[:100]


def get_zip_size_mb(zip_path: Path) -> float:
    return zip_path.stat().st_size / (1024 * 1024)


async def github_show_repos(update, ctx, url):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_owner(uid):
        ok_p, _ = is_premium(uid)
        if not ok_p:
            await update.message.reply_text(
                f"🚫 *Access Denied*\n\nID: `{uid}`", parse_mode="Markdown")
            return

    username = extract_github_username(url)
    if not username:
        await update.message.reply_text("❌ GitHub username detect nahi hua.")
        return

    msg = await update.message.reply_text(
        f"🔍 *Fetching repos for* `{username}` ...", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    repos, err = await loop.run_in_executor(None, fetch_all_repos, username)
    if err:
        await msg.edit_text(f"❌ {err}"); return
    if not repos:
        await msg.edit_text(f"📭 `{username}` ke koi public repos nahi."); return

    downloaded = load_history()
    new_repos = [r for r in repos if r["name"] not in downloaded]
    total_kb = sum((r.get("size") or 0) for r in repos)
    total_mb = total_kb / 1024

    summary = (
        f"👤 `{username}`\n\n"
        f"📦 Total repos: {len(repos)}\n"
        f"🆕 New: {len(new_repos)}\n"
        f"💾 Approx: ~{total_mb:.0f} MB\n\n"
        f"❓ *Range bhejo:*\n"
        f"• `1-50` → repos 1-50\n"
        f"• `500-600` → 500-600\n"
        f"• `200` → 1-200\n"
        f"• `1,5,10` → specific"
    )
    await msg.edit_text(summary, parse_mode="Markdown")
    WAITING_GITHUB[uid] = {"username": username, "repos": repos,
                           "chat_id": chat_id, "started_at": time.time()}


async def github_process_range(update, ctx, uid, text):
    state = WAITING_GITHUB.get(uid)
    if not state: return False
    repos = state["repos"]; total = len(repos)
    parsed = parse_range(text, total)
    if parsed is None:
        await update.message.reply_text(
            "❌ Invalid range. `1-50`, `500-600`, `200`, `1,5,10`",
            parse_mode="Markdown")
        return True
    if isinstance(parsed, list): indexes = parsed
    else:
        a, b = parsed; indexes = list(range(a, b + 1))
    if not indexes:
        await update.message.reply_text("❌ Koi repo select nahi hua.")
        WAITING_GITHUB.pop(uid, None); return True
    username = state["username"]
    WAITING_GITHUB.pop(uid, None)
    await update.message.reply_text(
        f"🚀 *Starting GitHub download*\n\n"
        f"👤 `{username}`\n📊 Range: #{indexes[0]} → #{indexes[-1]}\n"
        f"📦 Total: {len(indexes)}\n\n`/stop` bhej ke rok sakte ho.",
        parse_mode="Markdown")
    task = asyncio.create_task(
        github_download_batch(update, ctx, username, repos, indexes))
    CURRENT_TASK[uid] = task

    def _done(t):
        if CURRENT_TASK.get(uid) is t: CURRENT_TASK.pop(uid, None)
        STOP_FLAGS.pop(uid, None)

    task.add_done_callback(_done)
    return True


async def github_download_batch(update, ctx, username, repos, indexes):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    STOP_FLAGS[uid] = False
    send_ok = 0; skipped = 0; failed = 0
    loop = asyncio.get_event_loop()
    max_mb = get_max_upload_mb()

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{uid}")]
    ])

    status = await ctx.bot.send_message(
        chat_id,
        f"🐙 *GitHub Batch*\n\n👤 `{username}`\n📦 {len(indexes)} repos\n\n"
        f"Shuru kar raha hoon...",
        parse_mode="Markdown", reply_markup=cancel_kb)

    for counter, repo_idx in enumerate(indexes, 1):
        if is_stopped(uid): break
        repo = repos[repo_idx - 1]
        repo_name = repo["name"]; repo_url = repo["html_url"]
        size_str = format_size_kb(repo.get("size") or 0)

        await safe_edit(
            ctx.bot, chat_id, status.message_id,
            f"📦 Item {counter}/{len(indexes)}\n🐙 {repo_name}\n\n"
            f"⬇️ Downloading ZIP...", cancel_kb)

        zip_path, err = await loop.run_in_executor(
            None, download_github_repo, repo, GITHUB_DIR, None)

        if err or not zip_path:
            failed += 1
            try:
                await ctx.bot.send_message(
                    chat_id, f"❌ #{repo_idx:02d} `{repo_name}` — fail\n`{err}`",
                    parse_mode="Markdown")
            except Exception: pass
            continue
        if is_stopped(uid):
            try: zip_path.unlink()
            except: pass
            break
        size_mb = get_zip_size_mb(zip_path)

        if size_mb > max_mb:
            failed += 1
            link = repo_url
            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"⚠️ #{repo_idx:02d} `{repo_name}` skip — "
                    f"{size_mb:.1f} MB > {max_mb} MB\n🔗 [Open Repo]({link})",
                    parse_mode="Markdown", disable_web_page_preview=True)
            except Exception: pass
            await send_channel_notice(
                ctx, f"📁 *Repo:* `{repo_name}`\n👤 `{username}`\n"
                     f"⚠️ >{max_mb}MB — direct link\n🔗 {link}")
            try: zip_path.unlink()
            except: pass
            continue

        display_name = f"{repo_idx:02d}_{repo_name}.zip"
        caption = (f"📦 #{repo_idx:02d} — {repo_name}\n"
                   f"🔗 {repo_url}\n💾 {size_str}")
        sent_msg = None
        try:
            with open(zip_path, "rb") as f:
                sent_msg = await ctx.bot.send_document(
                    chat_id=chat_id, document=f, filename=display_name,
                    caption=caption, read_timeout=7200, write_timeout=7200)
            send_ok += 1
            save_to_history(repo_name)
            bump_stat("total_uploads", 1)

            # ✅ SILENT forward — no "Channel me bhi bhej diya!" notification
            if sent_msg:
                await forward_to_channel(ctx, chat_id, sent_msg.message_id)
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
        f"✅ Bheje: {send_ok}\n⏭️ Skip: {skipped}\n❌ Fail: {failed}\n"
        f"📦 Total: {len(indexes)}"
    )
    await safe_edit(ctx.bot, chat_id, status.message_id, final)


# ═══════════════════════════════════════════════════════════
#  👑 ADMIN PANEL
# ═══════════════════════════════════════════════════════════
SETTING_KEYS = {
    "api_id":          ("🔑 API ID",           "Telegram API ID"),
    "api_hash":        ("🔑 API Hash",         "Telegram API Hash"),
    "bot_token":       ("🤖 Bot Token",        "Requires restart"),
    "github_token":    ("🐙 GitHub Token",     "GitHub API access"),
    "channel_id":      ("📢 Channel ID",       "Forward destination"),
    "course_id":       ("📚 Course ID",        "Akamai course"),
    "fallback_userid": ("👤 Fallback User ID", "Akamai fallback"),
    "akamai_host":     ("🌐 Akamai Host",      "Video host"),
    "max_upload_mb":   ("📦 Max Upload MB",    "Upload limit"),
    "local_api_url":   ("🌐 Local API URL",    "Requires restart"),
}


def admin_panel_kb():
    rows = [
        [InlineKeyboardButton("🐙 GitHub Token", callback_data="adm:set:github_token")],
        [InlineKeyboardButton("📢 Channel ID", callback_data="adm:set:channel_id")],
        [InlineKeyboardButton("📚 Course ID", callback_data="adm:set:course_id"),
         InlineKeyboardButton("👤 Fallback UserID", callback_data="adm:set:fallback_userid")],
        [InlineKeyboardButton("🌐 Akamai Host", callback_data="adm:set:akamai_host")],
        [InlineKeyboardButton("🔑 API ID", callback_data="adm:set:api_id"),
         InlineKeyboardButton("🔑 API Hash", callback_data="adm:set:api_hash")],
        [InlineKeyboardButton("🤖 Bot Token", callback_data="adm:set:bot_token")],
        [InlineKeyboardButton("📦 Max Upload MB", callback_data="adm:set:max_upload_mb")],
        [InlineKeyboardButton("🌐 Local API URL", callback_data="adm:set:local_api_url")],
        [InlineKeyboardButton("👑 Premium Manager", callback_data="menu:premium"),
         InlineKeyboardButton("🍪 Cookies", callback_data="menu:cookies")],
        [InlineKeyboardButton("👥 All Users List", callback_data="adm:allusers")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("📢 Broadcast to All", callback_data="adm:broadcast")],
        [InlineKeyboardButton("🔄 View All Settings", callback_data="adm:view")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_view_settings_text():
    lines = ["⚙️ *Current Settings*\n"]
    for key, (label, desc) in SETTING_KEYS.items():
        val = get_setting(key)
        if val is None:
            default_map = {
                "api_id": DEFAULT_API_ID, "api_hash": DEFAULT_API_HASH,
                "bot_token": DEFAULT_BOT_TOKEN,
                "github_token": DEFAULT_GITHUB_TOKEN or "(not set)",
                "channel_id": DEFAULT_CHANNEL_ID,
                "course_id": DEFAULT_COURSE_ID,
                "fallback_userid": DEFAULT_FALLBACK_USERID,
                "akamai_host": DEFAULT_AKAMAI_HOST,
                "max_upload_mb": DEFAULT_MAX_UPLOAD_MB,
                "local_api_url": os.environ.get("LOCAL_API_URL", "(not set)")
            }
            v = default_map.get(key, "(not set)")
            src = "env"
        else:
            v = val
            src = "db"
        if key in ("bot_token", "api_hash", "github_token") and v and len(str(v)) > 12:
            v = f"{str(v)[:8]}...{str(v)[-4:]}"
        lines.append(f"• *{label}* [{src}]\n  `{v}`")
    return "\n".join(lines)[:4000]


async def admin_panel_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    await update.message.reply_text(
        "👑 *Admin Control Panel*\n\n"
        "Koi bhi setting change karo. DB me save hoti hai.\n\n"
        "⚠️ Bot Token / API ID / API Hash / Local API URL change karne ke baad restart chahiye.",
        parse_mode="Markdown",
        reply_markup=admin_panel_kb())


async def admin_setting_cb(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not is_owner(uid):
        await q.edit_message_text("🚫 Sirf owner."); return
    data = q.data
    if not data.startswith("adm:"): return
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "stats":
        total_prem = len(list_premium_users())
        active = 0; now = now_utc()
        for u in list_premium_users():
            exp = u.get("expires")
            if exp and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp and exp > now:
                active += 1
        hist_len = len(load_history())
        await q.edit_message_text(
            f"📊 *Bot Statistics*\n\n"
            f"👥 Total users: {count_all_users()}\n"
            f"👑 Premium users: {total_prem} (active: {active})\n"
            f"📤 Total uploads: {get_stat('total_uploads', 0)}\n"
            f"📜 GitHub history: {hist_len}\n"
            f"🤖 Mode: {'4GB' if USE_LOCAL_API else '50MB'}\n"
            f"📢 Channel: `{get_channel_id()}`",
            parse_mode="Markdown",
            reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        return

    if action == "allusers":
        users = list_all_users()
        if not users:
            await q.edit_message_text("📭 Koi user nahi.",
                                      reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
            return
        lines = [f"👥 *All Users ({len(users)})*\n"]
        now = now_utc()
        for i, u in enumerate(users[:60], 1):
            uid_ = u.get("user_id", "?")
            uname = u.get("username", "")
            fname = u.get("first_name", "") or "?"
            ok_p, days = is_premium(uid_)
            tag = f"👑{days}d" if ok_p else "—"
            u_tag = f"@{uname}" if uname else fname[:15]
            lines.append(f"{i}. `{uid_}` | {u_tag} | {tag}")
        if len(users) > 60:
            lines.append(f"\n_...aur {len(users)-60} users_")
        lines.append(f"\n\nTotal: {len(users)}")
        text = "\n".join(lines)[:4000]
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        return

    if action == "panel":
        await q.edit_message_text(
            "👑 *Admin Control Panel*\n\nKya change karna hai?",
            parse_mode="Markdown", reply_markup=admin_panel_kb())
        return

    if action == "view":
        await q.edit_message_text(
            admin_view_settings_text(), parse_mode="Markdown",
            reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        return

    if action == "broadcast":
        WAITING_BROADCAST[uid] = True
        await q.edit_message_text(
            "📢 *Broadcast to ALL Users*\n\n"
            "Jo message bhejna hai woh next message me bhejo.\n"
            "Sab captured users ko jaayega.\n\n"
            "`/cancel` se rok do.",
            parse_mode="Markdown",
            reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        return

    if action == "set":
        if len(parts) < 3:
            await q.edit_message_text("❌ Invalid key."); return
        key = parts[2]
        if key not in SETTING_KEYS:
            await q.edit_message_text("❌ Unknown key."); return
        label, desc = SETTING_KEYS[key]
        current = get_setting(key)
        current_display = "`(not set)`"
        if current is not None:
            cv = str(current)
            if key in ("bot_token", "api_hash", "github_token") and len(cv) > 12:
                cv = f"{cv[:8]}...{cv[-4:]}"
            current_display = f"`{cv}`"
        WAITING_ADMIN_SETTING[uid] = {"key": key}
        await q.edit_message_text(
            f"✏️ *Change {label}*\n\n📝 {desc}\n"
            f"🔹 Current: {current_display}\n\n"
            f"Ab naya value bhejo.\n\n"
            f"• `/cancel` — cancel\n"
            f"• `/reset` — DB se delete (env default use hoga)",
            parse_mode="Markdown",
            reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        return


async def admin_reset_cmd(update, ctx):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("🚫 Sirf owner."); return
    state = WAITING_ADMIN_SETTING.get(uid)
    if not state:
        await update.message.reply_text("ℹ️ Koi setting edit nahi chal raha.")
        return
    key = state["key"]
    WAITING_ADMIN_SETTING.pop(uid, None)
    del_setting(key)
    await update.message.reply_text(
        f"✅ `{key}` reset to env-default.",
        parse_mode="Markdown",
        reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))


async def admin_set_settings_cmd(update, ctx):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("🚫 Sirf owner."); return
    if not ctx.args or len(ctx.args) < 2:
        keys = " | ".join(SETTING_KEYS.keys())
        await update.message.reply_text(
            f"Usage: `/set <key> <value>`\n\nKeys: `{keys}`",
            parse_mode="Markdown")
        return
    key = ctx.args[0]
    value = " ".join(ctx.args[1:])
    if key not in SETTING_KEYS:
        await update.message.reply_text(f"❌ Unknown key: `{key}`",
                                        parse_mode="Markdown")
        return
    if key in ("channel_id", "course_id", "fallback_userid", "max_upload_mb"):
        try: int(value)
        except ValueError:
            await update.message.reply_text("❌ Number expected."); return
    if set_setting(key, value):
        v = value
        if key in ("bot_token", "api_hash", "github_token") and len(v) > 12:
            v = f"{v[:8]}...{v[-4:]}"
        note = ""
        if key in ("bot_token", "api_id", "api_hash", "local_api_url"):
            note = "\n\n⚠️ *Restart required* for this change."
        await update.message.reply_text(
            f"✅ *{SETTING_KEYS[key][0]}* updated!\n\n`{v}`{note}",
            parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Save fail.")


async def admin_get_settings_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    await update.message.reply_text(
        admin_view_settings_text(), parse_mode="Markdown",
        reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))


# ═══════════════════════════════════════════════════════════
#  🎛️ MENU KEYBOARDS
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
            InlineKeyboardButton("👑 Admin Panel", callback_data="adm:panel"),
            InlineKeyboardButton("🍪 Cookies", callback_data="menu:cookies"),
        ])
        rows.append([
            InlineKeyboardButton("👥 All Users", callback_data="adm:allusers"),
            InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast"),
        ])
        rows.append([
            InlineKeyboardButton("👑 Premium Manager", callback_data="menu:premium"),
        ])
    rows.append([InlineKeyboardButton("🛑 STOP current batch",
                                      callback_data="menu:stop")])
    return InlineKeyboardMarkup(rows)


def github_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐙 Download Profile Repos", callback_data="gh:profile")],
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
        [InlineKeyboardButton("➖ Remove Premium User", callback_data="pm:remove")],
        [InlineKeyboardButton("📋 List Premium Users", callback_data="pm:list")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])


def back_kb(target="menu:main", label="🔙 Back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])


# ═══════════════════════════════════════════════════════════
#  🎛️ MENU CALLBACK
# ═══════════════════════════════════════════════════════════
async def menu_callback(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; data = q.data

    if data == "menu:main":
        await q.edit_message_text(
            "🏠 *Main Menu*\n\nKya karna hai? 👇",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(is_owner(uid)))
        return

    if data == "menu:github":
        await q.edit_message_text(
            "🐙 *GitHub Menu*\n\nChoose 👇",
            parse_mode="Markdown", reply_markup=github_menu_kb())
        return

    if data == "menu:caption":
        await q.edit_message_text(build_caption_menu(uid),
                                  reply_markup=caption_keyboard(uid))
        return

    if data == "menu:cookies":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb()); return
        await q.edit_message_text(
            "🍪 *Cookies Menu*\n\nManage 👇",
            parse_mode="Markdown", reply_markup=cookies_menu_kb())
        return

    if data == "menu:premium":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb()); return
        await q.edit_message_text(
            "👑 *Premium Manager*\n\nChoose 👇",
            parse_mode="Markdown", reply_markup=premium_menu_kb())
        return

    if data == "menu:txt":
        if not is_owner(uid):
            ok_p, _ = is_premium(uid)
            if not ok_p:
                await q.edit_message_text("🚫 Premium chahiye.",
                                          reply_markup=back_kb()); return
        WAITING_TXT[uid] = True
        await q.edit_message_text(
            "📄 *TXT Batch Upload*\n\n.txt file bhejo.",
            parse_mode="Markdown", reply_markup=back_kb())
        return

    if data == "menu:url":
        if not is_owner(uid):
            ok_p, _ = is_premium(uid)
            if not ok_p:
                await q.edit_message_text("🚫 Premium chahiye.",
                                          reply_markup=back_kb()); return
        WAITING_URL[uid] = True
        await q.edit_message_text(
            "🔗 *Direct URL Upload*\n\nURL(s) bhejo.",
            parse_mode="Markdown", reply_markup=back_kb())
        return

    if data == "menu:status":
        mode = "🚀 4GB" if USE_LOCAL_API else "⚠️ 50MB"
        db = "✅" if MONGO_OK else "❌"
        ck = ("✅" if (COOKIES_FILE.exists()
                      and COOKIES_FILE.stat().st_size > 0) else "❌")
        if is_owner(uid): access = "👑 Owner"
        else:
            ok_p, days = is_premium(uid)
            access = f"✅ Premium ({days}d)" if ok_p else "🚫 No Premium"
        await q.edit_message_text(
            f"📊 *Your Status*\n\n🆔 ID: `{uid}`\n🎫 {access}\n"
            f"🔧 Mode: {mode}\n📦 Max: {get_max_upload_mb()} MB\n"
            f"🗄 DB: {db} | 🍪 Cookies: {ck}\n"
            f"📢 Channel: {'✅' if get_channel_id() else '❌'}",
            parse_mode="Markdown", reply_markup=back_kb())
        return

    if data == "menu:help":
        await q.edit_message_text(
            "❓ *Help*\n\n"
            "• 📤 Upload TXT — TXT with URLs\n"
            "• 🔗 Upload URL — Direct URLs\n"
            "• 🐙 GitHub — Download/search\n"
            "• 🎨 Caption — Custom captions\n"
            "• 📊 Status — Your info\n"
            "• 🛑 Stop — Cancel batch",
            parse_mode="Markdown", reply_markup=back_kb())
        return

    if data == "menu:stop":
        STOP_FLAGS[uid] = True
        task = CURRENT_TASK.get(uid)
        if task and not task.done():
            await q.edit_message_text("🛑 *Stop requested!*",
                                      parse_mode="Markdown", reply_markup=back_kb())
        else:
            await q.edit_message_text("ℹ️ Koi active batch nahi.",
                                      reply_markup=back_kb())
        return

    # GitHub
    if data == "gh:profile":
        WAITING_GITHUB_PROFILE[uid] = True
        await q.edit_message_text(
            "🐙 *Download Profile Repos*\n\nUsername ya URL bhejo.",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return
    if data == "gh:search":
        WAITING_GITHUB_SEARCH[uid] = True
        await q.edit_message_text("🔍 *Search Repos*\n\nKeyword bhejo.",
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return
    if data == "gh:user":
        WAITING_GITHUB_USER[uid] = True
        await q.edit_message_text("👤 *User Info*\n\nUsername bhejo.",
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return
    if data == "gh:file":
        WAITING_GITHUB_FILE[uid] = True
        await q.edit_message_text("📁 *Search Files*\n\nFilename bhejo.",
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return
    if data == "gh:code":
        WAITING_GITHUB_CODE[uid] = True
        await q.edit_message_text("💻 *Search Code*\n\nKeyword bhejo.",
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return
    if data == "gh:history":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
            return
        hist = load_history()
        if not hist:
            await q.edit_message_text("📭 History empty.",
                                      reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
            return
        lines = [f"📜 *GitHub History* ({len(hist)})\n"]
        for i, name in enumerate(sorted(hist)[:50], 1):
            lines.append(f"{i}. `{name}`")
        if len(hist) > 50: lines.append(f"\n_...aur {len(hist)-50}_")
        lines.append("\nUse `/ghhistory clear` to wipe.")
        await q.edit_message_text("\n".join(lines)[:4000],
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:github", "🔙 GitHub Menu"))
        return

    # Cookies
    if data == "ck:status":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:cookies")); return
        ck_data = load_cookies_from_db()
        if not ck_data:
            await q.edit_message_text("📭 Koi cookies nahi.",
                                      reply_markup=back_kb("menu:cookies")); return
        updated = ck_data.get("updated_at")
        size = ck_data.get("size", 0)
        when = updated.strftime("%d %b %Y, %I:%M %p") if updated else "?"
        await q.edit_message_text(
            f"🍪 *Cookies Status*\n\n📁 `{ck_data.get('filename','cookies.txt')}`\n"
            f"💾 {size} bytes\n⏰ {when}",
            parse_mode="Markdown", reply_markup=back_kb("menu:cookies"))
        return
    if data == "ck:delete":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:cookies")); return
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
            "1️⃣ Chrome extension *'Get cookies.txt LOCALLY'* install karo\n"
            "2️⃣ Login karo YouTube/Instagram\n"
            "3️⃣ Export → cookies.txt\n"
            "4️⃣ `/setcookies` bhejo aur reply me file attach karo",
            parse_mode="Markdown", reply_markup=back_kb("menu:cookies"))
        return

    # Premium
    if data == "pm:add":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:premium")); return
        WAITING_PREMIUM_ADD[uid] = True
        await q.edit_message_text(
            "➕ *Add Premium*\n\nFormat: `<user_id> <days>`\nExample: `123456789 30`",
            parse_mode="Markdown",
            reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        return
    if data == "pm:remove":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:premium")); return
        WAITING_PREMIUM_REMOVE[uid] = True
        await q.edit_message_text("➖ *Remove Premium*\n\nUser ID bhejo.",
                                  parse_mode="Markdown",
                                  reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        return
    if data == "pm:list":
        if not is_owner(uid):
            await q.edit_message_text("🚫 Sirf owner.",
                                      reply_markup=back_kb("menu:premium")); return
        users = list_premium_users()
        if not users:
            await q.edit_message_text("📭 Koi user nahi.",
                                      reply_markup=back_kb("menu:premium")); return
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
#  🎨 CAPTION
# ═══════════════════════════════════════════════════════════
def build_caption_menu(uid):
    cap = get_user_caption(uid)
    status = "Enabled" if cap["enabled"] else "Disabled"
    return (
        "Set Caption\n\n➤ Variables 📌\n\n"
        "🎙 {file_name}\n📦 {file_size}\n⚙️ {file_extension}\n"
        "⏱ {file_duration}\n🔗 {file_url}\n🔢 {file_index}\n"
        "📚 {batch_name}\n👤 {downloaded_by}\n\n"
        "═══════════════════════\n\n➤ Current:\n"
        f"{cap['template']}\n\n═══════════════════════\n\n➤ Default:\n"
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
              WAITING_URL, WAITING_TXT, WAITING_ADMIN_SETTING, WAITING_BROADCAST):
        if d.get(uid):
            d.pop(uid, None); cleared = True
    if cleared:
        await update.message.reply_text("❌ Cancelled.")
    else:
        await update.message.reply_text("ℹ️ Kuch cancel nahi.")


# ═══════════════════════════════════════════════════════════
#  👑 OWNER COMMANDS
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
    try: target = int(ctx.args[0])
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


async def allusers_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    users = list_all_users()
    if not users:
        await update.message.reply_text("📭 Koi user nahi."); return
    lines = [f"👥 *All Users ({len(users)})*\n"]
    for i, u in enumerate(users[:80], 1):
        uid_ = u.get("user_id", "?")
        uname = u.get("username", "")
        fname = u.get("first_name", "") or "?"
        ok_p, days = is_premium(uid_)
        tag = f"👑{days}d" if ok_p else "—"
        u_tag = f"@{uname}" if uname else fname[:15]
        lines.append(f"{i}. `{uid_}` | {u_tag} | {tag}")
    if len(users) > 80:
        lines.append(f"\n_...aur {len(users)-80} users_")
    lines.append(f"\n\nTotal: {len(users)}")
    full = "\n".join(lines)
    for chunk in [full[i:i+3500] for i in range(0, len(full), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


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
            "1️⃣ Chrome extension *'Get cookies.txt LOCALLY'* install karo\n"
            "2️⃣ Login karo YouTube/Instagram\n"
            "3️⃣ Export → cookies.txt\n"
            "4️⃣ `/setcookies` bhejo + reply me file attach karo\n"
            "   YA seedha `cookies.txt` file bhejo",
            parse_mode="Markdown")
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("❌ .txt file bhejo"); return
    msg = await update.message.reply_text("📥 Cookies save kar raha...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()
        content = bytes(data)
        if len(content) < 50:
            await msg.edit_text("❌ File bahut chhoti"); return
        if save_cookies_to_db(content, doc.file_name or "cookies.txt"):
            COOKIES_FILE.write_bytes(content)
            await msg.edit_text(
                f"✅ *Cookies Saved!*\n\n📁 `{doc.file_name}`\n"
                f"💾 {len(content)} bytes",
                parse_mode="Markdown")
        else:
            await msg.edit_text("❌ MongoDB save fail")
    except Exception as e:
        await msg.edit_text(f"❌ Fail: {str(e)[:200]}")


async def getcookies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    data = load_cookies_from_db()
    if not data:
        await update.message.reply_text("📭 Koi cookies nahi."); return
    updated = data.get("updated_at")
    size = data.get("size", 0)
    when = updated.strftime("%d %b %Y, %I:%M %p") if updated else "?"
    await update.message.reply_text(
        f"🍪 *Cookies Status*\n\n📁 `{data.get('filename', 'cookies.txt')}`\n"
        f"💾 {size} bytes\n⏰ Updated: {when}",
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


async def replacetoken_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/replacetoken <github_token>`", parse_mode="Markdown")
        return
    new_token = ctx.args[0].strip()
    if set_setting("github_token", new_token):
        v = f"{new_token[:8]}...{new_token[-4:]}" if len(new_token) > 12 else new_token
        await update.message.reply_text(
            f"✅ *GitHub Token updated!*\n\n`{v}`\n\n_Turant kaam karega._",
            parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Save fail.")


async def tokenstatus_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    tk = get_github_token()
    if not tk:
        await update.message.reply_text("⚠️ No token set.",
                                        parse_mode="Markdown")
    else:
        v = f"{tk[:8]}...{tk[-4:]}" if len(tk) > 12 else tk
        await update.message.reply_text(f"🔑 Current token: `{v}`",
                                        parse_mode="Markdown")


async def githistory_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf owner."); return
    if ctx.args and ctx.args[0].lower() in ("clear", "reset", "wipe"):
        clear_history()
        await update.message.reply_text("✅ History cleared.")
        return
    hist = load_history()
    if not hist:
        await update.message.reply_text("📭 History empty."); return
    lines = [f"📜 *GitHub History* ({len(hist)} items)\n"]
    for i, name in enumerate(sorted(hist)[:80], 1):
        lines.append(f"{i}. `{name}`")
    if len(hist) > 80:
        lines.append(f"\n_...aur {len(hist)-80}_")
    full = "\n".join(lines)
    for chunk in [full[i:i+3500] for i in range(0, len(full), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


# ═══════════════════════════════════════════════════════════
#  🚀 START / HELP
# ═══════════════════════════════════════════════════════════
async def start(update, ctx):
    if is_duplicate(update.update_id): return
    uid = update.effective_user.id

    # 👥 AUTO CAPTURE USER
    capture_user(update.effective_user)

    mode = "🚀 4GB" if USE_LOCAL_API else "⚠️ 50MB"
    db = "✅" if MONGO_OK else "❌"
    ck = ("✅" if (COOKIES_FILE.exists()
                  and COOKIES_FILE.stat().st_size > 0) else "❌")
    if is_owner(uid): access = "👑 Owner"
    else:
        ok_p, days = is_premium(uid)
        access = f"✅ Premium ({days}d)" if ok_p else "🚫 No Premium"
    ch = "✅" if get_channel_id() else "❌"
    await update.message.reply_text(
        f"👋 *Course Uploader Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 Mode: {mode} | 📦 Max: {get_max_upload_mb()} MB\n"
        f"🎫 {access}\n"
        f"🗄 DB: {db} | 🍪 Cookies: {ck} | 📢 Channel: {ch}\n\n"
        f"📌 All uploads silently forward to channel.\n\n"
        f"👉 Menu se choose karo 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(is_owner(uid)))


async def help_cmd(update, ctx): await start(update, ctx)


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
    capture_user(update.effective_user)
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


async def handle_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_duplicate(update.update_id): return
    uid = update.effective_user.id
    capture_user(update.effective_user)
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


async def github_cmd(update, ctx):
    uid = update.effective_user.id
    capture_user(update.effective_user)
    if not is_owner(uid):
        ok_p, _ = is_premium(uid)
        if not ok_p:
            await update.message.reply_text("🚫 Premium chahiye."); return
    if ctx.args:
        url = ctx.args[0]
        if not url.startswith("http"): url = "https://" + url
        if is_github_profile_url(url):
            await github_show_repos(update, ctx, url); return
        else:
            await update.message.reply_text("❌ Valid GitHub profile URL nahi.")
            return
    await update.message.reply_text(
        "🐙 *GitHub Menu*\n\nChoose 👇",
        parse_mode="Markdown", reply_markup=github_menu_kb())


# ═══════════════════════════════════════════════════════════
#  📨 TEXT HANDLER
# ═══════════════════════════════════════════════════════════
async def handle_text(update, ctx):
    if is_duplicate(update.update_id): return
    uid = update.effective_user.id
    capture_user(update.effective_user)
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"): return

    if WAITING_ADMIN_SETTING.get(uid):
        state = WAITING_ADMIN_SETTING.pop(uid)
        key = state["key"]
        label = SETTING_KEYS[key][0]
        if key in ("channel_id", "course_id", "fallback_userid", "max_upload_mb"):
            try: int(text)
            except ValueError:
                await update.message.reply_text(
                    "❌ Number expected. Try again.", parse_mode="Markdown")
                WAITING_ADMIN_SETTING[uid] = state
                return
        if set_setting(key, text):
            v = text
            if key in ("bot_token", "api_hash", "github_token") and len(v) > 12:
                v = f"{v[:8]}...{v[-4:]}"
            note = ""
            if key in ("bot_token", "api_id", "api_hash", "local_api_url"):
                note = "\n\n⚠️ *Restart required* for this change."
            await update.message.reply_text(
                f"✅ *{label}* updated!\n\n`{v}`{note}",
                parse_mode="Markdown",
                reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        else:
            await update.message.reply_text("❌ Save fail.")
        return

    if WAITING_BROADCAST.get(uid):
        WAITING_BROADCAST.pop(uid, None)
        users = list_all_users()
        sent = 0; fail = 0
        status = await update.message.reply_text(
            f"📢 Broadcasting to {len(users)} users...")
        for u in users:
            try:
                target_id = int(u.get("user_id"))
                await ctx.bot.send_message(
                    target_id,
                    f"📢 *Broadcast*\n\n{text}",
                    parse_mode="Markdown")
                sent += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(
            f"✅ *Broadcast Done!*\n\n✔️ Sent: {sent}\n❌ Failed: {fail}",
            parse_mode="Markdown",
            reply_markup=back_kb("adm:panel", "🔙 Admin Panel"))
        return

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
        if handled: return

    if WAITING_GITHUB_PROFILE.get(uid):
        WAITING_GITHUB_PROFILE.pop(uid, None)
        url = text if text.startswith("http") else f"https://github.com/{text}"
        if is_github_profile_url(url):
            await github_show_repos(update, ctx, url); return
        else:
            await update.message.reply_text("❌ Valid GitHub username nahi."); return

    if WAITING_GITHUB_SEARCH.get(uid):
        WAITING_GITHUB_SEARCH.pop(uid, None)
        ctx.args = text.split()
        await github_search_cmd(update, ctx); return

    if WAITING_GITHUB_USER.get(uid):
        WAITING_GITHUB_USER.pop(uid, None)
        ctx.args = [text.split()[0]]
        await github_user_cmd(update, ctx); return

    if WAITING_GITHUB_FILE.get(uid):
        WAITING_GITHUB_FILE.pop(uid, None)
        ctx.args = text.split()
        await github_file_cmd(update, ctx); return

    if WAITING_GITHUB_CODE.get(uid):
        WAITING_GITHUB_CODE.pop(uid, None)
        ctx.args = text.split()
        await github_code_cmd(update, ctx); return

    if WAITING_PREMIUM_ADD.get(uid):
        WAITING_PREMIUM_ADD.pop(uid, None)
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Format: `<user_id> <days>`", parse_mode="Markdown"); return
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
                target, f"🎉 *Premium Activated!*\n\n+{days} days\nExpires: {exp_str}",
                parse_mode="Markdown")
        except: pass
        return

    if WAITING_PREMIUM_REMOVE.get(uid):
        WAITING_PREMIUM_REMOVE.pop(uid, None)
        try: target = int(text.split()[0])
        except Exception:
            await update.message.reply_text("❌ user_id number."); return
        if remove_premium(target):
            await update.message.reply_text(
                f"✅ `{target}` removed.", parse_mode="Markdown",
                reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        else:
            await update.message.reply_text(
                f"ℹ️ `{target}` nahi tha.", parse_mode="Markdown",
                reply_markup=back_kb("menu:premium", "🔙 Premium Menu"))
        return

    if WAITING_URL.get(uid):
        WAITING_URL.pop(uid, None)
        if not URL_RE.search(text):
            await update.message.reply_text("❌ Koi URL nahi mila."); return
        items = parse_txt(text)
        if not items:
            await update.message.reply_text("❌ Koi valid URL nahi mila."); return
        await update.message.reply_text(f"✅ {len(items)} item(s). Shuru...")
        await start_batch(update, ctx, items, batch_label="Direct URL")
        return

    if is_github_profile_url(text):
        await github_show_repos(update, ctx, text); return

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
#  🐙 GitHub search/user/file/code
# ═══════════════════════════════════════════════════════════
async def github_search_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        ok_p, _ = is_premium(update.effective_user.id)
        if not ok_p: return
    if not ctx.args:
        await update.message.reply_text("Usage: `/search <kw>`",
                                        parse_mode="Markdown"); return
    keyword = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(
        f"🔍 Searching: `{esc(keyword)}`...", parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(
                f"https://api.github.com/search/repositories"
                f"?q={quote(keyword)}&per_page=20&sort=stars",
                headers=github_headers(), timeout=20, **get_requests_kwargs())
            if r.status_code != 200:
                return None, f"Search failed ({r.status_code})"
            return r.json().get("items", []), None
        except Exception as e:
            return None, str(e)[:120]

    items, err = await loop.run_in_executor(None, _do)
    if err: await msg.edit_text(f"❌ {err}"); return
    if not items: await msg.edit_text("❌ No repos."); return
    text = f"🔍 *Top {len(items)} repos:* `{esc(keyword)}`\n\n"
    for i, repo in enumerate(items, 1):
        desc = esc((repo.get("description") or "No desc")[:100])
        text += (f"*{i}. {esc(repo['full_name'])}* ⭐{repo['stargazers_count']}\n"
                 f"📝 {desc}\n🔗 {repo['html_url']}\n\n")
    for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk)


async def github_user_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        ok_p, _ = is_premium(update.effective_user.id)
        if not ok_p: return
    if not ctx.args:
        await update.message.reply_text("Usage: `/user <name>`",
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
                return None, f"Not found ({r.status_code})"
            return r.json(), None
        except Exception as e:
            return None, str(e)[:120]

    u, err = await loop.run_in_executor(None, _do)
    if err: await msg.edit_text(f"❌ {err}"); return
    text = (
        f"👤 *{esc(u.get('login'))}*\n"
        f"📝 Name: {esc(u.get('name') or 'N/A')}\n"
        f"🏢 Company: {esc(u.get('company') or 'N/A')}\n"
        f"📍 Location: {esc(u.get('location') or 'N/A')}\n"
        f"📧 Email: {esc(u.get('email') or 'N/A')}\n"
        f"🔗 Blog: {esc(u.get('blog') or 'N/A')}\n"
        f"📦 Repos: {u.get('public_repos')}\n"
        f"👥 Followers: {u.get('followers')} | Following: {u.get('following')}\n"
        f"🔗 {u.get('html_url')}"
    )
    await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def github_file_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        ok_p, _ = is_premium(update.effective_user.id)
        if not ok_p: return
    if not ctx.args:
        await update.message.reply_text("Usage: `/file <filename>`",
                                        parse_mode="Markdown"); return
    keyword = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(f"🔍 `{esc(keyword)}`...",
                                          parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(
                f"https://api.github.com/search/code"
                f"?q=filename:{quote(keyword)}&per_page=20",
                headers=github_headers(), timeout=20, **get_requests_kwargs())
            if r.status_code != 200:
                return None, f"Failed ({r.status_code}) — valid token needed"
            return r.json().get("items", []), None
        except Exception as e:
            return None, str(e)[:120]

    items, err = await loop.run_in_executor(None, _do)
    if err: await msg.edit_text(f"❌ {err}"); return
    if not items: await msg.edit_text("❌ No files."); return
    text = f"📁 *Files:* `{esc(keyword)}`\n\n"
    for i, item in enumerate(items, 1):
        text += (f"*{i}. {esc(item['name'])}*\n"
                 f"📂 {esc(item['repository']['full_name'])}\n"
                 f"📄 `{esc(item['path'])}`\n🔗 {item['html_url']}\n\n")
    for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk)


async def github_code_cmd(update, ctx):
    if not is_owner(update.effective_user.id):
        ok_p, _ = is_premium(update.effective_user.id)
        if not ok_p: return
    if not ctx.args:
        await update.message.reply_text("Usage: `/code <kw>`",
                                        parse_mode="Markdown"); return
    keyword = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(f"🔍 `{esc(keyword)}`...",
                                          parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def _do():
        try:
            r = requests.get(
                f"https://api.github.com/search/code?q={quote(keyword)}&per_page=20",
                headers=github_headers(), timeout=20, **get_requests_kwargs())
            if r.status_code != 200:
                return None, f"Failed ({r.status_code}) — valid token needed"
            return r.json().get("items", []), None
        except Exception as e:
            return None, str(e)[:120]

    items, err = await loop.run_in_executor(None, _do)
    if err: await msg.edit_text(f"❌ {err}"); return
    if not items: await msg.edit_text("❌ No code matches."); return
    text = f"💻 *Code:* `{esc(keyword)}`\n\n"
    for i, item in enumerate(items, 1):
        text += (f"*{i}. {esc(item['name'])}*\n"
                 f"📂 {esc(item['repository']['full_name'])}\n"
                 f"📄 `{esc(item['path'])}`\n🔗 {item['html_url']}\n\n")
    for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk)


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
        BotCommand("stop", "Stop current batch"),
        BotCommand("cancel", "Cancel waiting state"),
        BotCommand("caption", "Custom caption setup"),
        BotCommand("premium", "Check premium status"),
        BotCommand("myid", "Get your Telegram ID"),
    ]
    owner_commands = general_commands + [
        BotCommand("admin", "👑 Admin Panel"),
        BotCommand("set", "Set setting: /set <key> <value>"),
        BotCommand("getsettings", "View all settings"),
        BotCommand("reset", "Reset setting to default"),
        BotCommand("add", "Add premium user"),
        BotCommand("remove", "Remove premium user"),
        BotCommand("list", "List premium users"),
        BotCommand("allusers", "List ALL users"),
        BotCommand("setcookies", "Upload cookies"),
        BotCommand("getcookies", "Cookies status"),
        BotCommand("delcookies", "Delete cookies"),
        BotCommand("replacetoken", "Quick set GitHub token"),
        BotCommand("tokenstatus", "Show GitHub token"),
        BotCommand("ghhistory", "GitHub history"),
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
    BOT_TOKEN = get_bot_token()
    builder = (Application.builder()
               .token(BOT_TOKEN)
               .concurrent_updates(16)
               .post_init(post_init))
    if USE_LOCAL_API:
        builder = (builder.base_url(LOCAL_API_BASE)
                          .base_file_url(LOCAL_FILE_BASE)
                          .local_mode(True))
    app = builder.build()

    # Core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("txt", txt_cmd))
    app.add_handler(CommandHandler("caption", caption_cmd))
    app.add_handler(CommandHandler("cancel", cancel_caption_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))

    # GitHub
    app.add_handler(CommandHandler("github", github_cmd))
    app.add_handler(CommandHandler("search", github_search_cmd))
    app.add_handler(CommandHandler("user", github_user_cmd))
    app.add_handler(CommandHandler("file", github_file_cmd))
    app.add_handler(CommandHandler("code", github_code_cmd))
    app.add_handler(CommandHandler("ghhistory", githistory_cmd))

    # Admin
    app.add_handler(CommandHandler("admin", admin_panel_cmd))
    app.add_handler(CommandHandler("set", admin_set_settings_cmd))
    app.add_handler(CommandHandler("getsettings", admin_get_settings_cmd))
    app.add_handler(CommandHandler("reset", admin_reset_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("allusers", allusers_cmd))
    app.add_handler(CommandHandler("setcookies", setcookies_cmd))
    app.add_handler(CommandHandler("getcookies", getcookies_cmd))
    app.add_handler(CommandHandler("delcookies", delcookies_cmd))
    app.add_handler(CommandHandler("replacetoken", replacetoken_cmd))
    app.add_handler(CommandHandler("tokenstatus", tokenstatus_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(admin_setting_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^gh:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^ck:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^pm:"))
    app.add_handler(CallbackQueryHandler(caption_callback, pattern=r"^cap:"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^cancel:"))

    # Messages
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
                f"Channel: {get_channel_id()} | Volume: {VOLUME_DIR}"
            )
            app.run_polling(allowed_updates=Update.ALL_TYPES,
                            drop_pending_updates=True, close_loop=False)
            break
        except KeyboardInterrupt:
            log.info("👋 Exiting."); break
        except Exception as e:
            err = str(e)
            log.error(f"❌ Crash: {err}")
            if "Logged out" in err or "Unauthorized" in err:
                time.sleep(60)
            elif "Conflict" in err or "terminated by other" in err:
                time.sleep(30)
            else:
                time.sleep(10)


if __name__ == "__main__":
    run_bot()
