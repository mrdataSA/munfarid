"""Configuration loaded from the .env file in the project root.

Every external service is optional: a missing key disables that feature
and the rest of the system keeps working.
"""

import os
import secrets
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# Cloud: the bot + site run on Vercel (read-only files), collectors on GitHub Actions (CI)
ON_VERCEL = bool(os.environ.get("VERCEL"))
ON_CI = bool(os.environ.get("GITHUB_ACTIONS"))
if os.name != "nt" and (ON_VERCEL or ON_CI) and not os.environ.get("RIZQ_KEEP_TZ"):
    # Saudi time (UTC+3, no daylight saving) for "today", the 9 o'clock message and daily caps.
    # A POSIX rule, so no zoneinfo files are needed on the server.
    os.environ["TZ"] = "AST-3"
    time.tzset()
# RIZQ_DATA_DIR lets tests run against a copy of the data
DATA_DIR = os.environ.get("RIZQ_DATA_DIR") or os.path.join(ROOT, "data")
STORE_DIR = os.path.join(DATA_DIR, "store")      # private: never served over HTTP
CV_DIR = os.path.join(DATA_DIR, "cvs")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
DEMO_JOBS_FILE = os.path.join(ROOT, "data", "jobs.json")  # demo seed (read by the server only)
LOG_DIR = os.path.join(ROOT, "logs")
ENV_FILE = os.path.join(ROOT, ".env")

ENV_TEMPLATE = """# RIZQ settings. Edit values after the = sign, then restart start.bat.

# Admin dashboard password (open http://localhost:8080/#/admin)
ADMIN_PASSWORD={admin_password}

# Groq AI (free): https://console.groq.com/keys
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b
# Max Groq requests per day (free tier is ~1000)
GROQ_DAILY_LIMIT=900

# Telegram bot (free): create one with @BotFather, paste the token and the bot username (without @)
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
# Hour of the daily "new jobs" message (0-23), and max jobs posted per group/channel per day
BOT_DIGEST_HOUR=9
BOT_FEED_DAILY_CAP=80

# Optional: X (Twitter) API bearer token (reading requires a paid tier)
X_BEARER_TOKEN=
# X API is pay-per-use (~$0.005 per post read): max posts read per day
X_DAILY_POST_CAP=300

# Aggregator job APIs (free keys, legal coverage of big boards)
# Careerjet: https://www.careerjet.com.sa/partners/register/as-publisher
CAREERJET_API_KEY=
# Jooble: https://sa.jooble.org/api/about  (free plan = small total request budget)
JOOBLE_API_KEY=
JOOBLE_TOTAL_BUDGET=500

# LinkedIn guest jobs (slow and polite). OFF by default.
# Turn on (1) only while you hold LinkedIn's written permission (keep it saved).
LINKEDIN_ENABLED=0
LINKEDIN_DAILY_CAP=240

# Collect jobs automatically every N hours while the server runs (0 = off)
AUTO_COLLECT_HOURS=3

# Fast lane: Telegram channels + newest LinkedIn jobs every N minutes, for quick alerts (0 = off)
FAST_COLLECT_MINUTES=60

# Hide jobs older than N days
JOB_MAX_AGE_DAYS=45

# Most jobs kept (newest first); older ones are dropped
MAX_STORED_JOBS=15000

# Web port
PORT=8080
"""


def _ensure_env_file():
    """Create .env on first run with a random admin password (local only: in the cloud the
    settings come from environment variables and the files are read-only)."""
    if not os.path.exists(ENV_FILE) and not (ON_VERCEL or ON_CI):
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(ENV_TEMPLATE.format(admin_password=secrets.token_urlsafe(9)))


def _load_env():
    _ensure_env_file()
    values = {}
    if not os.path.exists(ENV_FILE):
        return values
    with open(ENV_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    return values


_ENV = _load_env()


def get(key, default=""):
    """Environment variables override the .env file."""
    return os.environ.get(key) or _ENV.get(key) or default


def get_int(key, default):
    try:
        return int(get(key, str(default)))
    except ValueError:
        return default


ADMIN_PASSWORD = get("ADMIN_PASSWORD")
GROQ_API_KEY = get("GROQ_API_KEY")
GROQ_MODEL = get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_DAILY_LIMIT = get_int("GROQ_DAILY_LIMIT", 900)
TELEGRAM_BOT_TOKEN = get("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_USERNAME = get("TELEGRAM_BOT_USERNAME").lstrip("@")
BOT_DIGEST_HOUR = min(max(get_int("BOT_DIGEST_HOUR", 9), 0), 23)   # daily new-jobs message (local time)
BOT_FEED_DAILY_CAP = get_int("BOT_FEED_DAILY_CAP", 80)            # max jobs posted per group/channel per day
X_BEARER_TOKEN = get("X_BEARER_TOKEN")
X_DAILY_POST_CAP = get_int("X_DAILY_POST_CAP", 300)
CAREERJET_API_KEY = get("CAREERJET_API_KEY")
CAREERJET_USER_IP = get("CAREERJET_USER_IP", "127.0.0.1")
JOOBLE_API_KEY = get("JOOBLE_API_KEY")
JOOBLE_TOTAL_BUDGET = get_int("JOOBLE_TOTAL_BUDGET", 500)
LINKEDIN_ENABLED = get("LINKEDIN_ENABLED", "0") == "1"
LINKEDIN_DAILY_CAP = get_int("LINKEDIN_DAILY_CAP", 240)
AUTO_COLLECT_HOURS = get_int("AUTO_COLLECT_HOURS", 3)
FAST_COLLECT_MINUTES = get_int("FAST_COLLECT_MINUTES", 60)
JOB_MAX_AGE_DAYS = get_int("JOB_MAX_AGE_DAYS", 45)
MAX_STORED_JOBS = max(1000, get_int("MAX_STORED_JOBS", 15000))
PORT = get_int("PORT", 8080)

# Cloud storage: shared data (subscribers, settings, AI cache...) lives in Postgres (Supabase).
# Empty = local JSON files only.
DATABASE_URL = get("DATABASE_URL")
# Read-only copy of the collected jobs bundled with each Vercel deployment (tools/publish.py)
SNAPSHOT_DIR = get("SNAPSHOT_DIR") or (os.path.join(ROOT, "snapshot") if ON_VERCEL else "")
# Public site address(es), e.g. munfarid.vercel.app (comma separated); checked against Host/Origin
PUBLIC_HOSTS = [h.strip().lower() for h in get("PUBLIC_HOSTS").split(",") if h.strip()]
# Secret part of the Telegram webhook URL + header Telegram must send back
TELEGRAM_WEBHOOK_SECRET = get("TELEGRAM_WEBHOOK_SECRET")
if ON_VERCEL:
    CV_DIR = "/tmp/rizq-cvs"   # the deployment is read-only; the CV text itself is kept in the database


def webhook_header_secret():
    """Telegram sends this in X-Telegram-Bot-Api-Secret-Token. It differs from the URL part
    (request paths appear in Vercel's logs, headers do not)."""
    import hashlib
    import hmac
    if not TELEGRAM_WEBHOOK_SECRET:
        return ""
    return hmac.new(TELEGRAM_WEBHOOK_SECRET.encode(), b"telegram-header", hashlib.sha256).hexdigest()

# Polite, honest client identification for collectors (no personal data)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 RizqJobsBot/1.0")


def _has_pypdf():
    import importlib.util
    return importlib.util.find_spec("pypdf") is not None


def features():
    """Which optional integrations are configured (for the admin dashboard)."""
    return {
        "groq": bool(GROQ_API_KEY),
        "telegram_bot": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME),
        "pdf_cv": _has_pypdf(),
        "x_api": bool(X_BEARER_TOKEN),
        "linkedin": LINKEDIN_ENABLED,
        "careerjet": bool(CAREERJET_API_KEY),
        "jooble": bool(JOOBLE_API_KEY),
        "auto_collect_hours": AUTO_COLLECT_HOURS,
    }
