"""
Discord webhook notifications for NetPulse AI alerts.

Runs beside SQLite alerting — failures never interrupt monitoring.

Cooldown keys look like ``OUTAGE:example.com`` so repeated DOWN/high-latency edges
do not spam Discord within ``NOTIFICATION_COOLDOWN_MINUTES``.

Target configuration events (``TARGET_*``) use a separate purple-style embed and no
outage cooldown — user actions are infrequent.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests


TARGET_EVENT_TYPES = frozenset(
    {"TARGET_ADDED", "TARGET_REMOVED", "TARGET_ENABLED", "TARGET_DISABLED"}
)


def _utc_readable(ts_iso: Optional[str] = None) -> str:
    if ts_iso and ts_iso.strip():
        return ts_iso.strip()
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class NotificationService:
    """Discord webhook delivery + simple per-target/type cooldown."""

    def __init__(
        self,
        *,
        webhook_url: str,
        enabled: bool,
        cooldown_minutes: int,
        public_app_url: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._webhook_url = (webhook_url or "").strip()
        self._enabled = enabled
        self._cooldown_seconds = max(0, int(cooldown_minutes)) * 60
        self._public_app_url = (public_app_url or "").strip().rstrip("/")
        self._timeout = timeout_seconds

        self._lock = threading.Lock()
        self._last_sent: Dict[str, float] = {}
        self._logged_missing_webhook = False

    def is_configured(self) -> bool:
        """True when a webhook URL is present (never exposes URL outside logs)."""
        return bool(self._webhook_url)

    def _warn_missing_webhook_once(self) -> None:
        if not self._logged_missing_webhook:
            print(
                "Discord webhook not configured; skipping notification.",
                flush=True,
            )
            self._logged_missing_webhook = True

    def _cooldown_key(self, alert_type: str, target: str) -> str:
        return f"{alert_type}:{target}"

    def should_send_notification(self, alert_type: str, target: str) -> bool:
        """
        True if notifications are on, webhook exists, and cooldown elapsed for this key.

        Does not send HTTP — only answers whether we'd attempt Discord now.
        """
        if not self._enabled:
            return False
        if not self.is_configured():
            return False
        key = self._cooldown_key(alert_type, target)
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self._cooldown_seconds:
                return False
        return True

    def _mark_sent(self, alert_type: str, target: str) -> None:
        key = self._cooldown_key(alert_type, target)
        with self._lock:
            self._last_sent[key] = time.monotonic()

    def _embed_color(self, alert_type: str) -> int:
        # Discord embed colors are integers (decimal RGB).
        if alert_type == "OUTAGE":
            return 15158332  # red-ish
        if alert_type == "HIGH_LATENCY":
            return 16753920  # orange-ish
        return 5814783  # muted blue

    def _target_event_color(self) -> int:
        """Purple accent — distinct from outage (red) and latency (orange)."""
        return 9412388  # ~ #8f74d4

    def send_target_event(
        self,
        event_type: str,
        target: str,
        message: str,
        timestamp: Optional[str] = None,
    ) -> bool:
        """
        Notify Discord about target configuration changes (add/remove/enable/disable).

        Does not use outage cooldown. Missing webhook logs once (shared with alerts).
        Never raises.
        """
        if not self._enabled:
            return False
        if event_type not in TARGET_EVENT_TYPES:
            return False

        if not self.is_configured():
            self._warn_missing_webhook_once()
            return False

        ts = _utc_readable(timestamp)
        fields = [
            {"name": "Event", "value": event_type, "inline": True},
            {"name": "Target", "value": target[:256], "inline": True},
            {"name": "Time", "value": ts, "inline": False},
            {"name": "Detail", "value": message[:1024], "inline": False},
        ]
        if self._public_app_url:
            fields.append(
                {
                    "name": "Dashboard",
                    "value": self._public_app_url + "/targets",
                    "inline": False,
                }
            )

        payload: Dict[str, Any] = {
            "username": "NetPulse AI",
            "embeds": [
                {
                    "title": "NetPulse AI Target Update",
                    "description": "Monitoring configuration changed.",
                    "color": self._target_event_color(),
                    "fields": fields,
                }
            ],
        }

        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code in (200, 204):
                return True
            print(
                f"[NetPulse AI / Discord] Unexpected status {resp.status_code}: "
                f"{resp.text[:200]}",
                flush=True,
            )
        except requests.RequestException as exc:
            print(f"[NetPulse AI / Discord] Target event request failed: {exc}", flush=True)
        return False

    def send_discord_alert(
        self,
        alert_type: str,
        target: str,
        message: str,
        timestamp: Optional[str] = None,
    ) -> bool:
        """
        POST an alert to Discord if configured and cooldown allows.

        Returns True only after a successful HTTP response from Discord.
        Missing webhook / disabled notifications → silent skip except first missing-webhook log line.
        """
        if not self._enabled:
            return False

        if alert_type not in ("OUTAGE", "HIGH_LATENCY"):
            return False

        if not self.is_configured():
            self._warn_missing_webhook_once()
            return False

        if not self.should_send_notification(alert_type, target):
            return False

        ts = _utc_readable(timestamp)
        fields = [
            {"name": "Type", "value": alert_type, "inline": True},
            {"name": "Target", "value": target, "inline": True},
            {"name": "Time", "value": ts, "inline": False},
            {"name": "Detail", "value": message[:1024], "inline": False},
        ]
        if self._public_app_url:
            fields.append(
                {
                    "name": "Dashboard",
                    "value": self._public_app_url + "/",
                    "inline": False,
                }
            )

        payload: Dict[str, Any] = {
            "username": "NetPulse AI",
            "embeds": [
                {
                    "title": "NetPulse AI Alert",
                    "description": "Monitoring detected an issue.",
                    "color": self._embed_color(alert_type),
                    "fields": fields,
                }
            ],
        }

        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code in (200, 204):
                self._mark_sent(alert_type, target)
                return True
            print(
                f"[NetPulse AI / Discord] Unexpected status {resp.status_code}: "
                f"{resp.text[:200]}",
                flush=True,
            )
        except requests.RequestException as exc:
            print(f"[NetPulse AI / Discord] Request failed: {exc}", flush=True)
        return False

    def send_test_ping(self) -> Tuple[bool, str]:
        """
        Send a one-off test message (ignores cooldown).

        Returns (success, safe_message_without_secrets).
        """
        if not self.is_configured():
            return False, "Discord webhook is not configured."

        payload = {
            "username": "NetPulse AI",
            "embeds": [
                {
                    "title": "NetPulse AI — connection test",
                    "description": "If you see this message, Discord notifications are working.",
                    "color": 3447003,
                }
            ],
        }
        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code in (200, 204):
                return True, "Test message sent to Discord."
            return False, f"Discord returned HTTP {resp.status_code}."
        except requests.RequestException as exc:
            return False, f"Request failed: {type(exc).__name__}"

    def send_test_target_event(self) -> Tuple[bool, str]:
        """
        Sends a sample target-configuration embed (for ``/api/test-discord?type=target``).
        """
        if not self.is_configured():
            return False, "Discord webhook is not configured."

        ok = self.send_target_event(
            "TARGET_ADDED",
            "example.com",
            "This is a test of target configuration notifications.",
            timestamp=None,
        )
        if ok:
            return True, "Sample target update sent to Discord."
        return False, "Could not deliver sample target update (see server logs)."
