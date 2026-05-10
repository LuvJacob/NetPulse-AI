"""
NetPulse AI — Flask web app + API.

Phase 2 keeps routing in one file on purpose: easy to read top-to-bottom while learning.
Later you can split blueprints (`routes/status.py`, etc.) without changing DB/monitor code.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from flask import (
    Flask,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from config import Config
from database.db_manager import DatabaseManager, coerce_target_enabled_flag
from services.ai_service import GeminiAIService, google_generativeai_imported
from services.monitor_service import NetworkMonitor
from services.notification_service import NotificationService

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config.from_object(Config)

# Shared manager — created at import so ``flask --app app run`` works the same as ``python app.py``.
db_manager = DatabaseManager(Config.DATABASE_PATH)
db_manager.initialize_database(Config.DEFAULT_TARGETS)

notification_service = NotificationService(
    webhook_url=Config.DISCORD_WEBHOOK_URL,
    enabled=Config.NOTIFICATIONS_ENABLED,
    cooldown_minutes=max(0, Config.NOTIFICATION_COOLDOWN_MINUTES),
    public_app_url=Config.PUBLIC_APP_URL,
)

monitor = NetworkMonitor(
    db_manager,
    interval_seconds=Config.MONITOR_INTERVAL_SECONDS,
    http_timeout_seconds=Config.HTTP_TIMEOUT_SECONDS,
    latency_alert_threshold_ms=Config.LATENCY_ALERT_THRESHOLD_MS,
    notification_service=notification_service,
)
monitor.start_monitoring_loop()

ai_service = GeminiAIService(
    db_manager,
    api_key=Config.GEMINI_API_KEY,
    model_name=Config.GEMINI_MODEL,
    log_limit=Config.AI_LOG_LIMIT,
    alert_limit=Config.AI_ALERT_LIMIT,
    context_hours=Config.AI_CONTEXT_HOURS,
    cache_ttl_seconds=Config.AI_CACHE_TTL_SECONDS,
)


@app.context_processor
def inject_navigation() -> Dict[str, Any]:
    """Highlight the active sidebar link from the current Flask endpoint name."""
    mapping = {
        "dashboard": "dashboard",
        "alerts_page": "alerts",
        "targets_page": "targets",
        "logs_page": "logs",
        "settings_page": "settings",
    }
    ep = request.endpoint
    return {"active_nav": mapping.get(ep, "")}


def build_dashboard_targets() -> List[Dict[str, Any]]:
    """Merge latest probes with rolling-window stats for the UI + JSON APIs."""
    window_hrs = Config.METRICS_WINDOW_HOURS
    enriched: List[Dict[str, Any]] = []
    for row in db_manager.get_latest_statuses():
        tid = int(row["target_id"])
        enriched.append(
            {
                **row,
                "uptime_pct": db_manager.get_uptime_percentage(tid, hours=window_hrs),
                "avg_latency_ms": db_manager.get_average_latency(tid, hours=window_hrs),
                "outage_count": db_manager.get_outage_count(tid, hours=window_hrs),
            }
        )
    return enriched


@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        targets=build_dashboard_targets(),
        recent_alerts=db_manager.get_recent_alerts(limit=25),
        latency_threshold_ms=Config.LATENCY_ALERT_THRESHOLD_MS,
    )


@app.route("/alerts")
def alerts_page():
    return render_template(
        "alerts.html",
        alerts=db_manager.get_recent_alerts(limit=200),
    )


@app.route("/targets", methods=["GET", "POST"])
def targets_page():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "add":
            hostname = request.form.get("hostname") or ""
            try:
                new_id = db_manager.add_target(hostname)
                row = db_manager.get_target_by_id(new_id)
                host = str(row["target"]) if row else hostname.strip()
                try:
                    notification_service.send_target_event(
                        "TARGET_ADDED",
                        host,
                        f"{host} was added and is now being monitored.",
                    )
                except Exception:
                    print(
                        "[NetPulse AI / Discord] TARGET_ADDED notify failed; ignoring.",
                        flush=True,
                    )
                flash("Target added successfully.", "success")
            except ValueError as exc:
                flash(str(exc), "warning")
        elif action == "toggle_active":
            tid = request.form.get("target_id", type=int)
            enabled_raw = (request.form.get("enabled") or "").strip()
            if enabled_raw == "1":
                enabled = True
            elif enabled_raw == "0":
                enabled = False
            else:
                enabled = enabled_raw.lower() in ("true", "yes", "on")
            if tid is not None and tid > 0:
                row = db_manager.get_target_by_id(tid)
                if row is None:
                    flash("Target not found.", "warning")
                elif db_manager.set_target_enabled(tid, enabled):
                    host = str(row["target"])
                    event = "TARGET_ENABLED" if enabled else "TARGET_DISABLED"
                    detail = (
                        f"{host} monitoring was enabled."
                        if enabled
                        else f"{host} monitoring was disabled."
                    )
                    try:
                        notification_service.send_target_event(event, host, detail)
                    except Exception:
                        print(
                            "[NetPulse AI / Discord] target toggle notify failed; ignoring.",
                            flush=True,
                        )
                    flash(
                        "Target enabled." if enabled else "Target disabled.",
                        "success",
                    )
                else:
                    flash("Target not found.", "warning")
        elif action == "delete":
            tid = request.form.get("target_id", type=int)
            if tid is not None and tid > 0:
                row = db_manager.get_target_by_id(tid)
                host = str(row["target"]) if row else ""
                if row and db_manager.delete_target(tid):
                    try:
                        notification_service.send_target_event(
                            "TARGET_REMOVED",
                            host,
                            f"{host} was removed and is no longer being monitored.",
                        )
                    except Exception:
                        print(
                            "[NetPulse AI / Discord] TARGET_REMOVED notify failed; ignoring.",
                            flush=True,
                        )
                    flash("Target removed.", "success")
                else:
                    flash("Target not found.", "warning")
        return redirect(url_for("targets_page"))

    rows = db_manager.get_monitored_targets()
    target_rows = []
    for r in rows:
        d = dict(r)
        raw_active = d.get("is_active")
        coerced = coerce_target_enabled_flag(raw_active)
        d["is_active"] = coerced
        target_rows.append(d)
        if current_app.debug:
            print(
                "[Targets DEBUG]",
                f"id={d['id']} target={d['target']!r}",
                f"raw_is_active={raw_active!r}",
                f"coerced_is_active={coerced}",
                flush=True,
            )
    return render_template("targets.html", target_rows=target_rows)


@app.route("/logs")
def logs_page():
    raw_tid = request.args.get("target_id")
    if raw_tid in (None, ""):
        target_id = None
    else:
        try:
            target_id = int(raw_tid)
        except (TypeError, ValueError):
            target_id = None

    limit = request.args.get("limit", default=100, type=int) or 100
    limit = max(1, min(limit, 500))

    raw_hours = request.args.get("hours", default="24")
    if isinstance(raw_hours, str) and raw_hours.lower() == "all":
        hours = None
        selected_hours = None
    else:
        try:
            hours = int(raw_hours)
        except (TypeError, ValueError):
            hours = 24
        selected_hours = hours

    log_rows = db_manager.get_recent_logs(target_id=target_id, limit=limit, hours=hours)

    target_options = []
    for r in db_manager.get_monitored_targets():
        d = dict(r)
        d["is_active"] = coerce_target_enabled_flag(d.get("is_active"))
        target_options.append(d)
    return render_template(
        "logs.html",
        log_rows=log_rows,
        target_options=target_options,
        selected_target_id=target_id,
        selected_limit=limit,
        selected_hours=selected_hours,
    )


@app.route("/settings")
def settings_page():
    cfg = app.config
    settings = {
        "interval_seconds": cfg["MONITOR_INTERVAL_SECONDS"],
        "latency_threshold_ms": cfg["LATENCY_ALERT_THRESHOLD_MS"],
        "metrics_window_hours": cfg["METRICS_WINDOW_HOURS"],
        "http_timeout_seconds": cfg["HTTP_TIMEOUT_SECONDS"],
        "database_path": str(cfg["DATABASE_PATH"]),
        "gemini_model": cfg["GEMINI_MODEL"],
        "ai_cache_ttl_seconds": cfg["AI_CACHE_TTL_SECONDS"],
        "ai_context_hours": cfg["AI_CONTEXT_HOURS"],
        "gemini_configured": bool(cfg["GEMINI_API_KEY"]),
    }
    return render_template("settings.html", settings=settings)


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/api/status")
def api_status():
    """Latest snapshot per target plus uptime/latency/outage stats."""
    return jsonify(build_dashboard_targets())


@app.route("/api/logs")
def api_logs():
    """
    Recent monitoring samples (newest first).

    Query params:
    - target_id (optional): filter to one target
    - limit (optional, default 200): cap rows
    - hours (optional, default 24): rolling window; use ``all`` to disable the window filter
    """
    target_id = request.args.get("target_id", type=int)
    limit = request.args.get("limit", default=200, type=int) or 200

    raw_hours = request.args.get("hours", default="24")
    if isinstance(raw_hours, str) and raw_hours.lower() == "all":
        hours = None
    else:
        try:
            hours = int(raw_hours)
        except (TypeError, ValueError):
            hours = 24

    logs = db_manager.get_recent_logs(target_id=target_id, limit=limit, hours=hours)
    return jsonify(logs)


@app.route("/api/alerts")
def api_alerts():
    limit = request.args.get("limit", default=50, type=int) or 50
    rows = db_manager.get_recent_alerts(limit=limit)
    return jsonify(rows)


@app.route("/api/ai-debug")
def api_ai_debug():
    """
    Temporary diagnostics — does not expose secrets.
    Remove or protect if you deploy publicly.
    """
    raw = os.getenv("GEMINI_API_KEY") or ""
    stripped = raw.strip()
    return jsonify(
        {
            "dotenv_loaded": bool(Config.DOTENV_LOADED),
            "gemini_key_present": bool(stripped),
            "gemini_key_length": len(stripped),
            "google_generativeai_imported": google_generativeai_imported(),
        }
    )


@app.route("/api/test-discord", methods=["GET"])
def api_test_discord():
    """
    Sends a single test embed to Discord when ``DISCORD_WEBHOOK_URL`` is set.

    Query ``type=target`` — sample **NetPulse AI Target Update** embed instead of ping.

    Does not return or log the webhook URL.
    """
    mode = (request.args.get("type") or "").strip().lower()
    if mode == "target":
        ok, msg = notification_service.send_test_target_event()
    else:
        ok, msg = notification_service.send_test_ping()
    return jsonify({"status": "success" if ok else "error", "message": msg})


@app.route("/api/list-gemini-models", methods=["GET"])
def api_list_gemini_models():
    """
    Lists models visible to your API key via ``genai.list_models()`` (names + generation methods).
    Prints the same list to the Flask terminal. Does not expose the API key.
    """
    return jsonify(ai_service.list_models_debug())


@app.route("/api/test-gemini", methods=["GET"])
def test_gemini():
    """
    Connectivity check: prompt \"Say hello in one sentence.\"
    Uses ``GeminiAIService.connectivity_test`` — same Gemini setup as ``/api/ai-summary``.

    Query: optional ``model`` — must match an id from ``GET /api/list-gemini-models`` if set;
    otherwise the configured ``GEMINI_MODEL`` is used with automatic fallback.

    **Registered on this Flask ``app`` instance at import time** — restart Flask after edits.
    """
    model_query = request.args.get("model")
    return jsonify(ai_service.connectivity_test(model_query))


@app.route("/api/ai-summary")
def api_ai_summary():
    """
    Gemini-powered network summary (bounded context + server-side cache).

    Query params:
    - refresh=1 — bypass cache TTL and call Gemini again.
    """
    force = request.args.get("refresh", default="", type=str).lower() in (
        "1",
        "true",
        "yes",
    )
    payload = ai_service.summarize_network_health(
        force_refresh=force,
        debug=current_app.debug,
    )
    return jsonify(payload)


if __name__ == "__main__":
    # Debug reloader disabled — prevents duplicate background monitors.
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
