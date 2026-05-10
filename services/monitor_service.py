"""
Background network monitor.

We probe with HTTP GET (``requests``), not ICMP ping, because many websites block ICMP
but still serve HTTPS — this matches “is the service reachable?” for operators.

The long-running loop runs in a **daemon thread** so Flask keeps answering browsers quickly.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlparse

import requests

from database.db_manager import DatabaseManager

if TYPE_CHECKING:
    from services.notification_service import NotificationService


def normalize_url(hostname: str) -> str:
    hostname = hostname.strip()
    if not hostname:
        raise ValueError("empty hostname")
    if urlparse(hostname).scheme:
        return hostname
    return f"https://{hostname}"


class NetworkMonitor:
    """
    Periodically probes each configured target and persists results + alerts.

    ``ping_target`` naming matches how folks talk about checks; internally it's HTTP.
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        interval_seconds: int,
        http_timeout_seconds: float,
        latency_alert_threshold_ms: float = 300.0,
        session_factory: Callable[[], requests.Session] = requests.Session,
        notification_service: Optional["NotificationService"] = None,
    ) -> None:
        self._db = db_manager
        self._interval = interval_seconds
        self._timeout = http_timeout_seconds
        self._latency_threshold = latency_alert_threshold_ms
        self._session_factory = session_factory
        self._notifications = notification_service
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def ping_target(self, hostname: str, session: requests.Session) -> Tuple[Optional[float], str]:
        """
        Returns (latency_ms, status).

        UP  = TCP/TLS/HTTP stack returned a response (even 4xx/5xx counts as reachable).
        DOWN = DNS failure, timeout, or connection error.
        """
        url = normalize_url(hostname)
        try:
            resp = session.get(url, timeout=self._timeout)
            ms = resp.elapsed.total_seconds() * 1000.0
            return ms, "UP"
        except requests.RequestException:
            return None, "DOWN"

    def monitor_once(self, session: requests.Session) -> None:
        """
        One full sweep across all targets.

        Alerts are intentionally simple:
        - OUTAGE when we transition into DOWN (avoids spamming identical alerts every 30s).
        - HIGH_LATENCY when we're UP but crossed above the threshold from a “not slow” prior sample.
        """
        for row in self._db.get_active_monitored_targets():
            target_id = int(row["id"])
            hostname = str(row["target"])

            # Snapshot previous state *before* inserting the newest measurement.
            prev = self._db.get_last_log_for_target(target_id)

            try:
                latency_ms, status = self.ping_target(hostname, session)
            except Exception:
                latency_ms, status = None, "DOWN"

            self._db.save_monitoring_result(
                target_id=target_id,
                response_time_ms=latency_ms,
                status=status,
            )

            prev_status = prev["status"] if prev else None

            if status == "DOWN":
                if prev_status != "DOWN":
                    msg = f"{hostname} is DOWN (no HTTP response)."
                    self._db.save_alert(
                        target=hostname,
                        alert_type="OUTAGE",
                        message=msg,
                    )
                    self._push_discord("OUTAGE", hostname, msg)
                continue

            # UP branch — latency warnings
            if latency_ms is None:
                continue

            # Match dashboard styling: treat latency at/above threshold as "slow".
            if latency_ms < self._latency_threshold:
                continue

            prev_latency = float(prev["response_time_ms"]) if prev and prev["response_time_ms"] is not None else None
            prev_was_slow = (
                prev_status == "UP"
                and prev_latency is not None
                and prev_latency >= self._latency_threshold
            )

            if not prev_was_slow:
                msg = (
                    f"{hostname} latency high: {latency_ms:.0f} ms "
                    f"(threshold {self._latency_threshold:.0f} ms)."
                )
                self._db.save_alert(
                    target=hostname,
                    alert_type="HIGH_LATENCY",
                    message=msg,
                )
                self._push_discord("HIGH_LATENCY", hostname, msg)

    def _push_discord(self, alert_type: str, target: str, message: str) -> None:
        """Best-effort Discord ping — never raises."""
        if self._notifications is None:
            return
        try:
            self._notifications.send_discord_alert(
                alert_type,
                target,
                message,
                timestamp=None,
            )
        except Exception:
            print("[NetPulse AI / Discord] Notification helper raised; ignoring.", flush=True)

    def start_monitoring_loop(self) -> None:
        """Spawn daemon thread; safe to call twice — ignored if already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="netpulse-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        session = self._session_factory()
        while not self._stop.is_set():
            try:
                self.monitor_once(session)
            except Exception:
                # Never let the worker thread die quietly — next interval retries.
                pass
            self._stop.wait(self._interval)
