"""
Application configuration.

We keep settings in one module so the same code can run in dev vs production
later (e.g. different DB paths) without hunting through the codebase.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Project root (folder containing config.py) — same folder as netpulse.sqlite3 and .env
BASE_DIR = Path(__file__).resolve().parent
_DOTENV_PATH = BASE_DIR / ".env"

# Load .env from project root; returns True if variables were loaded from the environment.
DOTENV_LOADED = load_dotenv(_DOTENV_PATH)


class Config:
    """Default configuration — override via environment variables where noted."""

    # Whether python-dotenv loaded variables from BASE_DIR / ".env"
    DOTENV_LOADED = DOTENV_LOADED

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")

    # SQLite file lives beside the project — simple for Phase 1; Docker/K8s later.
    DATABASE_PATH = BASE_DIR / os.environ.get("NETPULSE_DB", "netpulse.sqlite3")

    # Seconds between monitoring rounds for all targets.
    MONITOR_INTERVAL_SECONDS = int(os.environ.get("MONITOR_INTERVAL", "30"))

    # Phase 1 seed targets (hostnames — we normalize to https:// in the monitor).
    DEFAULT_TARGETS = ("google.com", "ibm.com", "att.com")

    # HTTP probe: treat responses within this many seconds as "attempt finished".
    HTTP_TIMEOUT_SECONDS = 10

    # Phase 2 — alerts + dashboard aggregates (rolling window).
    LATENCY_ALERT_THRESHOLD_MS = float(os.environ.get("LATENCY_ALERT_MS", "300"))
    METRICS_WINDOW_HOURS = int(os.environ.get("METRICS_WINDOW_HOURS", "24"))

    # --- Google Gemini (AI summaries) ---
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    AI_LOG_LIMIT = int(os.environ.get("AI_LOG_LIMIT", "100"))
    AI_ALERT_LIMIT = int(os.environ.get("AI_ALERT_LIMIT", "20"))
    AI_CONTEXT_HOURS = int(os.environ.get("AI_CONTEXT_HOURS", "24"))
    AI_CACHE_TTL_SECONDS = int(os.environ.get("AI_CACHE_TTL_SECONDS", "300"))

    # --- Notifications (Phase 4B — Discord webhook) ---
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    NOTIFICATIONS_ENABLED = os.environ.get(
        "NOTIFICATIONS_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no", "off")
    try:
        NOTIFICATION_COOLDOWN_MINUTES = int(
            os.environ.get("NOTIFICATION_COOLDOWN_MINUTES", "10")
        )
    except ValueError:
        NOTIFICATION_COOLDOWN_MINUTES = 10
    # Optional link shown in Discord embeds (e.g. https://your-host or http://127.0.0.1:5000).
    PUBLIC_APP_URL = os.environ.get("NETPULSE_PUBLIC_URL", "").strip().rstrip("/")
