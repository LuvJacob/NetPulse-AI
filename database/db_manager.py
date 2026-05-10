"""
SQLite database manager — one place for schema, writes, and reads.

Why isolate this from Flask?
- Flask only cares about HTTP; SQL details stay here.
- The background monitor writes logs/alerts through the same API → no duplicated queries.

Connections are short-lived (open → query → commit → close) with a timeout so the
monitor thread and Flask requests rarely fight over SQLite's write lock.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import urlparse
from typing import Any, Dict, Generator, Iterable, List, Optional

# DNS hostname length limit (RFC 1035-style practical cap).
_MAX_HOSTNAME_LEN = 253

# IPv4 dotted quad (monitor probes https://… — IPs allowed).
_IPV4_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})){3}$"
)

# Lowercase hostname labels + dots; single-label ok (e.g. localhost).
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _window_start_iso(hours: int) -> str:
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    return start.strftime("%Y-%m-%d %H:%M:%S")


def normalize_target_hostname(hostname: str) -> str:
    """
    Accept bare hostnames or full URLs; store a lowercase hostname for stable UNIQUE checks.
    """
    raw = (hostname or "").strip()
    if not raw:
        raise ValueError("Target cannot be empty.")
    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or parsed.netloc.split("@")[-1]
    if not raw:
        raise ValueError("Invalid target.")
    raw = raw.strip("/").split("/")[0]
    if not raw:
        raise ValueError("Invalid target.")
    host = raw.lower()
    # Strip trailing :port for IPv4 / hostname (avoid breaking bracketed IPv6).
    port_suffix = re.match(r"^(.+):(\d+)$", host)
    if port_suffix:
        base = port_suffix.group(1)
        if not base.startswith("["):
            host = base
    validate_target_hostname(host)
    return host


def validate_target_hostname(host: str) -> None:
    """
    Reject obvious junk while staying beginner-friendly (hostnames + IPv4).

    Raises ValueError with a short message for the Targets form flash.
    """
    if len(host) > _MAX_HOSTNAME_LEN:
        raise ValueError(f"Target is too long (max {_MAX_HOSTNAME_LEN} characters).")
    if ".." in host or host.startswith(".") or host.endswith("."):
        raise ValueError("Invalid hostname format (check dots).")
    if _IPV4_RE.match(host):
        return
    if not _HOSTNAME_RE.match(host):
        raise ValueError(
            "Use a hostname like example.com or an IPv4 address "
            "(letters, numbers, dots, hyphens only)."
        )


def coerce_target_enabled_flag(value: Any) -> int:
    """
    Normalize ``monitored_targets.is_active`` to ``0`` or ``1``.

    Jinja treats any non-empty string as truthy, so the string "0" would wrongly show as
    enabled in the UI unless coerced to integer 0 here.
    """
    if value is None:
        return 1
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return 1 if int(value) != 0 else 0
    except (TypeError, ValueError):
        s = str(value).strip().lower()
        return 1 if s in ("1", "true", "yes", "on") else 0


class DatabaseManager:
    """Thin wrapper around SQLite for targets, logs, alerts, and aggregates."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._schema_lock = threading.Lock()
        self._monitored_targets_schema_ok = False

    def ensure_monitored_targets_schema(self) -> None:
        """
        Legacy databases may lack ``is_active``. Add it, normalize NULL → 1.

        Safe for concurrent Flask + monitor thread (single lock). Fast no-op after success.

        Does not reset intentional ``is_active = 0`` rows—only NULL fixes.
        """
        if self._monitored_targets_schema_ok:
            return
        with self._schema_lock:
            if self._monitored_targets_schema_ok:
                return
            conn = sqlite3.connect(self._path, timeout=30.0)
            try:
                conn.row_factory = sqlite3.Row
                exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'monitored_targets'
                    LIMIT 1
                    """
                ).fetchone()
                if not exists:
                    return

                cols = {row[1] for row in conn.execute("PRAGMA table_info(monitored_targets)")}
                if "is_active" not in cols:
                    conn.execute(
                        """
                        ALTER TABLE monitored_targets
                        ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
                        """
                    )
                conn.execute(
                    """
                    UPDATE monitored_targets
                    SET is_active = 1
                    WHERE is_active IS NULL
                    """
                )
                conn.commit()

                cols_after = {
                    row[1] for row in conn.execute("PRAGMA table_info(monitored_targets)")
                }
                if "is_active" not in cols_after:
                    raise sqlite3.OperationalError(
                        "monitored_targets migration failed: is_active column missing"
                    )
                self._monitored_targets_schema_ok = True
            finally:
                conn.close()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        self.ensure_monitored_targets_schema()
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize_database(self, default_targets: Iterable[str]) -> None:
        """Create tables/indexes if missing and seed demo targets once."""
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitored_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
                );

                CREATE TABLE IF NOT EXISTS monitoring_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL REFERENCES monitored_targets(id) ON DELETE CASCADE,
                    response_time_ms REAL,
                    status TEXT NOT NULL CHECK (status IN ('UP', 'DOWN')),
                    packet_loss REAL,
                    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_logs_target_time
                    ON monitoring_logs(target_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(timestamp DESC);
                """
            )

        self.ensure_monitored_targets_schema()
        self._seed_targets_if_empty(tuple(default_targets))

    def _seed_targets_if_empty(self, hostnames: Iterable[str]) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM monitored_targets").fetchone()
            if row and row["c"] > 0:
                return
            conn.executemany(
                "INSERT INTO monitored_targets (target, created_at, is_active) VALUES (?, ?, 1)",
                [(h, _utc_now_iso()) for h in hostnames],
            )

    # --- Writes ---

    def save_monitoring_result(
        self,
        *,
        target_id: int,
        response_time_ms: Optional[float],
        status: str,
    ) -> None:
        """Append one probe result (every monitoring cycle calls this)."""
        if status not in ("UP", "DOWN"):
            raise ValueError("status must be UP or DOWN")
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO monitoring_logs (target_id, response_time_ms, status, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (target_id, response_time_ms, status, _utc_now_iso()),
            )

    def save_alert(self, *, target: str, alert_type: str, message: str) -> None:
        """Persist an alert row (email/SMS can subscribe later)."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO alerts (target, alert_type, message, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (target, alert_type, message, _utc_now_iso()),
            )

    # --- Target helpers ---

    def get_monitored_targets(self) -> List[sqlite3.Row]:
        """All configured targets (UI + logs dropdown): id, hostname, is_active."""
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT id, target, is_active
                FROM monitored_targets
                ORDER BY id ASC
                """
            ).fetchall()

    def get_target_by_id(self, target_id: int) -> Optional[Dict[str, Any]]:
        """One target row by id (for Discord messages before delete, etc.)."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, target, is_active
                FROM monitored_targets
                WHERE id = ?
                """,
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_active_monitored_targets(self) -> List[sqlite3.Row]:
        """Targets the background monitor should probe this cycle."""
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT id, target
                FROM monitored_targets
                WHERE is_active = 1
                ORDER BY id ASC
                """
            ).fetchall()

    def add_target(self, hostname: str) -> int:
        """Insert a new monitored target; raises ValueError if duplicate or invalid."""
        host = normalize_target_hostname(hostname)
        try:
            with self._connection() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO monitored_targets (target, created_at, is_active)
                    VALUES (?, ?, 1)
                    """,
                    (host, _utc_now_iso()),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("That target is already in the list.") from exc

    def set_target_enabled(self, target_id: int, enabled: bool) -> bool:
        """Enable (True) or pause (False) probing for one target."""
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE monitored_targets SET is_active = ? WHERE id = ?",
                (1 if enabled else 0, target_id),
            )
            return cur.rowcount > 0

    def delete_target(self, target_id: int) -> bool:
        """Remove a target and its logs (CASCADE). Returns True if a row was deleted."""
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM monitored_targets WHERE id = ?", (target_id,))
            return cur.rowcount > 0

    def get_last_log_for_target(self, target_id: int) -> Optional[sqlite3.Row]:
        """Most recent log row for a target — used for alert edge detection."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT response_time_ms, status, timestamp
                FROM monitoring_logs
                WHERE target_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (target_id,),
            ).fetchone()
            return row

    # --- Reads ---

    def get_latest_statuses(self) -> List[Dict[str, Any]]:
        """
        Latest probe per target plus hostname.

        Uses a correlated subquery so this works on older SQLite builds without window functions.
        """
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    t.id AS target_id,
                    t.target AS hostname,
                    t.is_active AS is_active,
                    l.response_time_ms,
                    l.status,
                    l.timestamp AS last_checked
                FROM monitored_targets t
                LEFT JOIN monitoring_logs l
                    ON l.id = (
                        SELECT id FROM monitoring_logs
                        WHERE target_id = t.id
                        ORDER BY timestamp DESC
                        LIMIT 1
                    )
                ORDER BY t.id ASC
                """
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["is_active"] = coerce_target_enabled_flag(d.get("is_active"))
            out.append(d)
        return out

    def get_recent_logs(
        self,
        *,
        target_id: Optional[int] = None,
        limit: int = 200,
        hours: Optional[int] = 24,
    ) -> List[Dict[str, Any]]:
        """
        Historical monitoring rows (newest first).

        When ``hours`` is None, no time filter is applied (good for small demo DBs).
        """
        limit = max(1, min(limit, 2000))
        params: List[Any] = []
        time_clause = ""
        if hours is not None:
            time_clause = "AND ml.timestamp >= ?"
            params.append(_window_start_iso(hours))
        target_clause = ""
        if target_id is not None:
            target_clause = "AND ml.target_id = ?"
            params.append(target_id)
        params.append(limit)

        query = f"""
            SELECT
                ml.id,
                ml.target_id,
                mt.target AS hostname,
                ml.response_time_ms,
                ml.status,
                ml.timestamp
            FROM monitoring_logs ml
            JOIN monitored_targets mt ON mt.id = ml.target_id
            WHERE 1=1 {time_clause} {target_clause}
            ORDER BY ml.timestamp DESC
            LIMIT ?
        """
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        # Friendly alias for APIs/charts (`hostname` remains for older callers).
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["target"] = d["hostname"]
            out.append(d)
        return out

    def get_recent_alerts(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, target, alert_type, message, timestamp
                FROM alerts
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Aggregates (rolling window, default 24h) ---

    def get_uptime_percentage(self, target_id: int, *, hours: int = 24) -> Optional[float]:
        """
        Percentage of probe samples that were UP in the window.

        Returns None when there are zero samples yet (dashboard can show “—”).
        """
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END) AS ups,
                    COUNT(*) AS total
                FROM monitoring_logs
                WHERE target_id = ? AND timestamp >= ?
                """,
                (target_id, _window_start_iso(hours)),
            ).fetchone()
        if not row or row["total"] == 0:
            return None
        return round(100.0 * row["ups"] / row["total"], 1)

    def get_average_latency(self, target_id: int, *, hours: int = 24) -> Optional[float]:
        """Average latency across UP samples that recorded a response time."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT AVG(response_time_ms) AS avg_ms
                FROM monitoring_logs
                WHERE target_id = ?
                  AND status = 'UP'
                  AND response_time_ms IS NOT NULL
                  AND timestamp >= ?
                """,
                (target_id, _window_start_iso(hours)),
            ).fetchone()
        if not row or row["avg_ms"] is None:
            return None
        return round(float(row["avg_ms"]), 1)

    def get_outage_count(self, target_id: int, *, hours: int = 24) -> int:
        """Count DOWN samples in the window (simple proxy for outage severity)."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM monitoring_logs
                WHERE target_id = ?
                  AND status = 'DOWN'
                  AND timestamp >= ?
                """,
                (target_id, _window_start_iso(hours)),
            ).fetchone()
        return int(row["c"]) if row else 0


def row_to_json_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize sqlite types for jsonify (everything JSON-serializable)."""
    return {k: (float(v) if isinstance(v, float) else v) for k, v in row.items()}
