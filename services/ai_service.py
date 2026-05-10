"""
Google Gemini integration for NetPulse AI.

All Gemini calls live here — Flask routes only delegate and return JSON.

Design:
- Small, bounded context (last N logs/alerts + per-target rollups).
- In-memory cache with TTL to avoid burning quota on every dashboard poll.
- Manual refresh bypasses TTL (still updates cache).
"""
from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

# Lazy import so ``python app.py`` still imports if the package is missing during tooling.
try:
    import google.generativeai as genai  # type: ignore
    _GENAI_IMPORT_OK = True
except ImportError:  # pragma: no cover
    genai = None  # type: ignore
    _GENAI_IMPORT_OK = False

try:
    from google.api_core import exceptions as google_api_exceptions  # type: ignore
except ImportError:  # pragma: no cover
    google_api_exceptions = None  # type: ignore


def google_generativeai_imported() -> bool:
    """For /api/ai-debug — True if the Gemini SDK can be imported."""
    return _GENAI_IMPORT_OK


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Treat docs-style placeholders as "no key" so users see the setup message, not a vague API error.
_PLACEHOLDER_KEYS = frozenset(
    {
        "your_key_here",
        "your_actual_gemini_key_here",
    }
)


def _normalize_gemini_api_key(raw: Optional[str]) -> Optional[str]:
    k = (raw or "").strip()
    if not k:
        return None
    if k.lower() in _PLACEHOLDER_KEYS:
        return None
    return k


def _err_payload(
    *,
    error_code: str,
    summary: str,
    gemini_configured: bool = True,
    cached: bool = False,
    debug: bool = False,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """Uniform error JSON (never includes API keys)."""
    out: Dict[str, Any] = {
        "summary": summary,
        "generated_at": _utc_iso(),
        "status": "error",
        "cached": cached,
        "gemini_configured": gemini_configured,
        "error": error_code,
        "error_code": error_code,
    }
    if debug and detail:
        out["detail"] = detail
    return out


def _log_gemini_exception(exc: BaseException, *, context: str = "") -> None:
    """Print the real SDK error to the Flask terminal (never prints API keys)."""
    prefix = "[NetPulse AI / Gemini]"
    if context:
        prefix = f"{prefix} {context}:"
    print(f"{prefix} Exception type: {type(exc).__name__}", flush=True)
    print(f"{prefix} Message: {exc}", flush=True)
    traceback.print_exc()


def _classify_gemini_exception(exc: BaseException) -> Tuple[str, str]:
    """
    Map exception → (error_code, safe user-facing summary).
    Codes: invalid_api_key, quota_exceeded, model_not_found, request_blocked,
           sdk_error, unknown_error
    """
    raw = str(exc)
    low = raw.lower()
    name = type(exc).__name__

    # google.api_core typed exceptions (preferred when available)
    if google_api_exceptions is not None:
        if isinstance(exc, google_api_exceptions.ResourceExhausted):
            return (
                "quota_exceeded",
                "Gemini quota exceeded. Wait or check usage limits in Google AI Studio / Cloud.",
            )
        if isinstance(exc, google_api_exceptions.NotFound):
            return (
                "model_not_found",
                "That Gemini model was not found for this API version. "
                "Call GET /api/list-gemini-models to see available names, or set GEMINI_MODEL to a listed id.",
            )
        if isinstance(exc, google_api_exceptions.PermissionDenied):
            return (
                "invalid_api_key",
                "Gemini denied access (permission). Verify GEMINI_API_KEY in .env.",
            )
        if isinstance(exc, google_api_exceptions.InvalidArgument):
            if "model" in low or "not found" in low:
                return (
                    "model_not_found",
                    "Invalid model id. Use GET /api/list-gemini-models or set GEMINI_MODEL to a model your key supports.",
                )
            return (
                "invalid_api_key",
                "Gemini rejected the request (often an invalid API key). Check .env.",
            )

    # SDK misuse / wrong install
    if isinstance(exc, (ImportError, AttributeError, TypeError)):
        return (
            "sdk_error",
            "Gemini SDK error. Ensure google-generativeai is up to date: pip install -U google-generativeai",
        )

    # Response blocked / safety (often raised when reading .text)
    if any(
        x in low
        for x in (
            "blocked",
            "safety",
            "no valid candidates",
            "finish_reason",
            "response.text",
            "candidate",
        )
    ) and any(b in low for b in ("block", "safety", "prohibited", "no parts")):
        return (
            "request_blocked",
            "Gemini blocked this response (safety/policy). Try shorter prompts or adjust content.",
        )

    if "model" in low and any(
        x in low for x in ("not found", "not supported", "404", "does not exist", "invalid model")
    ):
        return (
            "model_not_found",
            "The Gemini model name is invalid or not available for this API. "
            "Use GET /api/list-gemini-models to pick a model id.",
        )

    if name == "RuntimeError" and "empty_model_response" in raw:
        return (
            "request_blocked",
            "Gemini returned no usable text (blocked, filtered, or empty). Check server logs for finish_reason.",
        )

    if any(
        s in low
        for s in (
            "api key not valid",
            "invalid api key",
            "api_key_invalid",
            "incorrect api key",
            "bad api key",
            "malformed api key",
        )
    ):
        return (
            "invalid_api_key",
            "The Gemini API key was rejected. Create a key in Google AI Studio and update .env.",
        )

    if "permission" in name.lower() or "permissiondenied" in low:
        return (
            "invalid_api_key",
            "Gemini refused this API key. Verify GEMINI_API_KEY in .env.",
        )

    if "quota" in low or "resource_exhausted" in low or "429" in low:
        return (
            "quota_exceeded",
            "Gemini quota or rate limit hit. Wait and retry or check quotas.",
        )

    return (
        "unknown_error",
        "AI summary unavailable. Check the Flask terminal for the full Gemini traceback.",
    )


def _generation_config() -> Any:
    """google-generativeai accepts a dict or GenerationConfig for generation_config."""
    if genai is None:
        return {"temperature": 0.35, "max_output_tokens": 512}
    cfg_cls = getattr(genai, "GenerationConfig", None)
    if cfg_cls is None and hasattr(genai, "types"):
        cfg_cls = getattr(genai.types, "GenerationConfig", None)
    if callable(cfg_cls):
        try:
            return cfg_cls(temperature=0.35, max_output_tokens=512)
        except Exception:
            pass
    return {"temperature": 0.35, "max_output_tokens": 512}


def _extract_text_from_response(response: Any) -> Tuple[str, Optional[str]]:
    """
    Read response.text safely. Returns (text, issue_code).
    issue_code: 'blocked', 'empty', or None if OK.
    """
    try:
        t = getattr(response, "text", None)
        if t is not None:
            s = str(t).strip()
            if s:
                return s, None
    except Exception as exc:
        low = str(exc).lower()
        _log_gemini_exception(exc, context="reading response.text")
        if any(x in low for x in ("block", "safety", "candidate", "finish")):
            return "", "blocked"
        return "", "empty"

    # Fallback: inspect candidates / finish_reason
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return "", "empty"
    cand = cands[0]
    fr = getattr(cand, "finish_reason", None)
    fr_s = str(fr).upper()
    if "SAFETY" in fr_s or "BLOCK" in fr_s or "PROHIBITED" in fr_s:
        return "", "blocked"
    parts = getattr(cand, "content", None)
    parts = getattr(parts, "parts", None) if parts is not None else None
    if not parts:
        return "", "empty"
    chunks = []
    for p in parts:
        if hasattr(p, "text") and p.text:
            chunks.append(p.text)
    joined = "".join(chunks).strip()
    return joined, None if joined else "empty"


def _short_model_name(full_name: str) -> str:
    """``models/gemini-2.0-flash`` → ``gemini-2.0-flash`` for ``GenerativeModel``."""
    n = (full_name or "").strip()
    if n.startswith("models/"):
        return n[len("models/") :]
    return n


def _supports_generate_content(methods: List[str]) -> bool:
    """True if the Model supports text generation (handles enum / REST name variants)."""
    for raw in methods:
        low = str(raw).lower().replace("-", "").replace("_", "")
        if "generatecontent" in low:
            return True
    return False


def _model_entry_from_sdk(m: Any) -> Dict[str, Any]:
    name = getattr(m, "name", None) or ""
    methods: List[str] = []
    sm = getattr(m, "supported_generation_methods", None)
    if sm is not None:
        for method in sm:
            methods.append(str(method))
    return {"name": name, "supported_generation_methods": methods}


def fetch_model_catalog(api_key: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Lists models for the configured API key via ``genai.list_models()``.
    Returns (entries, None) on success, or ([], error_code) on failure.
    """
    if genai is None:
        return [], "sdk_missing"
    k = _normalize_gemini_api_key(api_key)
    if not k:
        return [], "invalid_api_key"
    try:
        genai.configure(api_key=k)
        out: List[Dict[str, Any]] = []
        for m in genai.list_models():
            out.append(_model_entry_from_sdk(m))
        return out, None
    except Exception as exc:
        _log_gemini_exception(exc, context="list_models")
        return [], "list_models_failed"


def _compatible_models_ordered(entries: List[Dict[str, Any]]) -> List[str]:
    """Preserve ``list_models()`` order; only models that support generateContent."""
    seen: Set[str] = set()
    ordered: List[str] = []
    for e in entries:
        methods = e.get("supported_generation_methods") or []
        if not _supports_generate_content(methods):
            continue
        short = _short_model_name(str(e.get("name") or ""))
        if not short or short in seen:
            continue
        seen.add(short)
        ordered.append(short)
    return ordered


def _pick_effective_model(
    preferred: str,
    entries: List[Dict[str, Any]],
    *,
    exclude: Optional[Set[str]] = None,
) -> Tuple[str, str]:
    """
    Pick a model id for ``GenerativeModel``.

    If the configured model appears in the catalog with generateContent, use it.
    Otherwise prefer the first **flash** model (free-tier friendly), then any generateContent model.
    Returns ("", reason) if nothing usable remains (all candidates excluded).
    """
    exclude = exclude or set()
    preferred_n = _short_model_name(preferred)
    ordered = _compatible_models_ordered(entries)

    if preferred_n and preferred_n not in exclude and preferred_n in ordered:
        return preferred_n, "configured_model_available"

    for short in ordered:
        if short in exclude:
            continue
        if "flash" in short.lower():
            return short, (
                "fallback_first_flash_with_generateContent"
                if preferred_n not in ordered
                else "fallback_flash_after_exclusions"
            )

    for short in ordered:
        if short not in exclude:
            return short, "fallback_first_generateContent_capable"

    if preferred_n and preferred_n not in exclude:
        return preferred_n, "no_catalog_using_configured_name"

    return "", "no_compatible_model_remaining"


def _is_model_not_found(exc: BaseException) -> bool:
    if google_api_exceptions is not None and isinstance(
        exc,
        google_api_exceptions.NotFound,
    ):
        return True
    low = str(exc).lower()
    return "not found" in low and "model" in low


def run_simple_gemini_test(api_key: str, model_name: str) -> Dict[str, Any]:
    """
    Minimal connectivity check for /api/test-gemini (single short prompt).
    """
    if genai is None:
        return {
            "status": "error",
            "error_code": "sdk_error",
            "message": "google-generativeai is not installed.",
            "exception_type": "ImportError",
        }
    k = _normalize_gemini_api_key(api_key)
    if not k:
        return {
            "status": "error",
            "error_code": "invalid_api_key",
            "message": "GEMINI_API_KEY is missing or placeholder.",
            "exception_type": None,
        }

    prompt = "Say hello in one sentence."
    try:
        genai.configure(api_key=k)
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        text, issue = _extract_text_from_response(response)
        if issue == "blocked":
            return {
                "status": "error",
                "error_code": "request_blocked",
                "message": "Gemini blocked or filtered the response (safety).",
                "exception_type": None,
                "model": model_name,
            }
        if not text:
            return {
                "status": "error",
                "error_code": "unknown_error",
                "message": "Gemini returned an empty response.",
                "exception_type": None,
                "model": model_name,
            }
        return {
            "status": "success",
            "response": text,
            "model": model_name,
        }
    except Exception as exc:
        _log_gemini_exception(exc, context="test-gemini")
        code, safe = _classify_gemini_exception(exc)
        return {
            "status": "error",
            "error_code": code,
            "message": f"{safe} ({type(exc).__name__}: {exc})",
            "exception_type": type(exc).__name__,
            "model": model_name,
        }


class GeminiAIService:
    """Build compact prompts and request short infrastructure summaries from Gemini."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        api_key: Optional[str],
        model_name: str,
        log_limit: int,
        alert_limit: int,
        context_hours: int,
        cache_ttl_seconds: int,
    ) -> None:
        self._db = db_manager
        self._api_key = _normalize_gemini_api_key(api_key)
        self._model_name = model_name
        self._log_limit = max(1, min(log_limit, 200))
        self._alert_limit = max(1, min(alert_limit, 50))
        self._context_hours = max(1, min(context_hours, 168))
        self._cache_ttl = max(60, cache_ttl_seconds)

        self._lock = threading.Lock()
        self._cache_summary: Optional[str] = None
        self._cache_generated_at: Optional[str] = None
        self._cache_error: Optional[str] = None
        # Cached result of ``genai.list_models()`` for resolution + fallbacks.
        self._catalog_cache: Optional[List[Dict[str, Any]]] = None
        self._last_effective_model: Optional[str] = None
        self._last_model_resolution: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _invalidate_catalog_cache(self) -> None:
        with self._lock:
            self._catalog_cache = None

    def _get_or_fetch_catalog(self) -> List[Dict[str, Any]]:
        """Thread-safe cache of model catalog for this process."""
        with self._lock:
            if self._catalog_cache is not None:
                return self._catalog_cache
        entries, _err = fetch_model_catalog(self._api_key or "")
        with self._lock:
            self._catalog_cache = entries
        return entries

    def list_models_debug(self) -> Dict[str, Any]:
        """
        For ``GET /api/list-gemini-models``: JSON catalog + terminal printout.
        """
        if not self._api_key:
            return {
                "models": [],
                "status": "error",
                "message": "GEMINI_API_KEY is missing or placeholder.",
            }
        if genai is None:
            return {
                "models": [],
                "status": "error",
                "message": "google-generativeai is not installed.",
            }
        entries, err = fetch_model_catalog(self._api_key)
        with self._lock:
            self._catalog_cache = entries
        print("[NetPulse AI / Gemini] list_models() — discovered models:", flush=True)
        if not entries:
            print("  (none returned)", flush=True)
        for e in entries:
            print(
                f"  name={e.get('name')}  "
                f"supported_generation_methods={e.get('supported_generation_methods')}",
                flush=True,
            )
        out: Dict[str, Any] = {"models": entries}
        if err:
            out["status"] = "error"
            out["message"] = err
        return out

    def connectivity_test(self, model_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Minimal Gemini round-trip for ``GET /api/test-gemini`` — same resolution path as
        ``summarize_network_health`` (catalog → pick flash-capable fallback if needed).
        """
        preferred = (model_name or self._model_name or "gemini-2.0-flash").strip()
        catalog = self._get_or_fetch_catalog()
        effective, resolution = _pick_effective_model(preferred, catalog)
        if not effective:
            return {
                "status": "error",
                "error_code": "model_not_found",
                "message": "No Gemini model with generateContent support was returned by list_models().",
                "exception_type": None,
                "resolution": resolution,
                "requested_model": _short_model_name(preferred),
            }
        result = run_simple_gemini_test(self._api_key or "", effective)
        if isinstance(result, dict):
            result["resolution"] = resolution
            result["requested_model"] = _short_model_name(preferred)
        return result

    def _has_probe_data(self) -> bool:
        """True if at least one monitoring_samples row exists (any time range)."""
        rows = self._db.get_recent_logs(target_id=None, limit=1, hours=None)
        return len(rows) > 0

    def get_recent_monitoring_context(self) -> Dict[str, Any]:
        """
        Pull bounded slices from SQLite — never the whole database.

        Returns logs (newest-first), alerts, and small per-target rollups for trends.
        """
        logs = self._db.get_recent_logs(
            target_id=None,
            limit=self._log_limit,
            hours=self._context_hours,
        )
        alerts = self._db.get_recent_alerts(limit=self._alert_limit)
        rollups = self._build_target_rollups()
        return {"logs": logs, "alerts": alerts, "rollups": rollups}

    def _build_target_rollups(self) -> List[Dict[str, Any]]:
        """One row per target: 24h uptime, avg latency, outage sample count."""
        out: List[Dict[str, Any]] = []
        for row in self._db.get_latest_statuses():
            tid = int(row["target_id"])
            out.append(
                {
                    "target": row["hostname"],
                    "last_status": row["status"],
                    "last_check_utc": row["last_checked"],
                    "uptime_pct_24h": self._db.get_uptime_percentage(tid, hours=self._context_hours),
                    "avg_latency_ms_24h": self._db.get_average_latency(tid, hours=self._context_hours),
                    "down_samples_24h": self._db.get_outage_count(tid, hours=self._context_hours),
                }
            )
        return out

    def build_network_summary_prompt(self, context: Dict[str, Any]) -> str:
        """Turn structured context into a compact, factual prompt."""
        logs: List[Dict[str, Any]] = context.get("logs") or []
        alerts: List[Dict[str, Any]] = context.get("alerts") or []
        rollups: List[Dict[str, Any]] = context.get("rollups") or []

        lines = [
            "You are a senior network operations analyst helping an operator understand HTTP probe results.",
            "Rules:",
            "- Base conclusions ONLY on the data below. Do not invent IPs, hops, carriers, or causes.",
            "- If data is sparse, say so briefly.",
            "- Output 2–4 short sentences, professional tone, present tense.",
            "- Mention targets by hostname when relevant.",
            "- Packet loss field is usually absent; do not claim packet loss unless listed.",
            "",
            f"=== Per-target summary ({self._context_hours}h window) ===",
        ]
        for r in rollups:
            up = r.get("uptime_pct_24h")
            lat = r.get("avg_latency_ms_24h")
            up_s = f"{up:.1f}%" if up is not None else "n/a"
            lat_s = f"{lat:.0f} ms" if lat is not None else "n/a"
            lines.append(
                f"- {r['target']}: last={r['last_status']} | "
                f"uptime~{up_s} | avg_latency~{lat_s} | "
                f"DOWN_samples={r.get('down_samples_24h', 0)}"
            )

        lines.append("")
        lines.append(f"=== Recent alerts (newest first, max {self._alert_limit}) ===")
        if not alerts:
            lines.append("(none)")
        else:
            for a in alerts:
                lines.append(
                    f"- {a.get('timestamp')} | {a.get('target')} | {a.get('alert_type')} | {a.get('message')}"
                )

        lines.append("")
        lines.append(f"=== Recent probe samples (newest first, max {self._log_limit}, compact) ===")
        if not logs:
            lines.append("(none)")
        else:
            for row in logs[: self._log_limit]:
                lat = row.get("response_time_ms")
                lat_s = f"{lat:.0f}" if lat is not None else "n/a"
                lines.append(
                    f"- {row.get('timestamp')} | {row.get('hostname')} | {row.get('status')} | {lat_s} ms"
                )

        lines.append("")
        lines.append("Write the concise network health summary now:")
        return "\n".join(lines)

    def summarize_network_health(
        self,
        *,
        force_refresh: bool = False,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """
        Return API-shaped dict: summary text, timestamp, status, cache hints.

        Uses cache unless ``force_refresh`` or cache expired.
        """
        # --- 1) API key ---
        if not self._api_key:
            return _err_payload(
                error_code="missing_api_key",
                summary="Gemini API key is missing. Add GEMINI_API_KEY to your .env file.",
                gemini_configured=False,
                debug=debug,
                detail="GEMINI_API_KEY is empty or still set to a placeholder value.",
            )

        # --- 2) SDK ---
        if genai is None:
            logger.error(
                "google-generativeai is not installed. Run: pip install google-generativeai"
            )
            return _err_payload(
                error_code="sdk_error",
                summary=(
                    "Gemini SDK is not installed. Run: pip install google-generativeai "
                    "then restart the Flask app."
                ),
                gemini_configured=True,
                debug=debug,
                detail="ImportError: google.generativeai",
            )

        # --- 3) Enough monitoring data ---
        if not self._has_probe_data():
            return _err_payload(
                error_code="insufficient_monitoring_data",
                summary=(
                    "AI summary unavailable because there is not enough monitoring data yet. "
                    "Let the monitor run for a few minutes."
                ),
                gemini_configured=True,
                debug=debug,
                detail="monitoring_logs table has no rows yet.",
            )

        # --- 4) Cache ---
        with self._lock:
            if (
                not force_refresh
                and self._cache_summary
                and self._cache_generated_at
            ):
                try:
                    gen_dt = datetime.strptime(
                        self._cache_generated_at,
                        "%Y-%m-%dT%H:%M:%SZ",
                    ).replace(tzinfo=timezone.utc)
                    age_s = (datetime.now(timezone.utc) - gen_dt).total_seconds()
                except (ValueError, TypeError):
                    age_s = self._cache_ttl + 1

                if age_s < self._cache_ttl:
                    return {
                        "summary": self._cache_summary,
                        "generated_at": self._cache_generated_at,
                        "status": "success",
                        "cached": True,
                        "gemini_configured": True,
                        "error_code": None,
                        "model": self._last_effective_model,
                        "model_resolution": self._last_model_resolution,
                    }

        context = self.get_recent_monitoring_context()
        prompt = self.build_network_summary_prompt(context)

        catalog = self._get_or_fetch_catalog()
        tried: Set[str] = set()
        effective, resolution = _pick_effective_model(
            self._model_name,
            catalog,
            exclude=tried,
        )
        response = None
        last_exc: Optional[BaseException] = None
        max_attempts = max(8, len(_compatible_models_ordered(catalog)) + 3)

        for _attempt in range(max_attempts):
            if not effective:
                return _err_payload(
                    error_code="model_not_found",
                    summary=(
                        "No Gemini model with generateContent support was found for your API key. "
                        "Open GET /api/list-gemini-models or adjust GEMINI_MODEL."
                    ),
                    gemini_configured=True,
                    debug=debug,
                    detail=resolution if debug else None,
                )
            try:
                genai.configure(api_key=self._api_key)
                gem_model = genai.GenerativeModel(effective)
                response = gem_model.generate_content(
                    prompt,
                    generation_config=_generation_config(),
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if _is_model_not_found(exc):
                    _log_gemini_exception(
                        exc,
                        context=(
                            "summarize_network_health NotFound "
                            f"model={effective}, picking fallback"
                        ),
                    )
                    tried.add(effective)
                    effective, resolution = _pick_effective_model(
                        self._model_name,
                        catalog,
                        exclude=tried,
                    )
                    continue
                _log_gemini_exception(exc, context="summarize_network_health / generate_content")
                logger.exception("Gemini generate_content failed (model=%s)", effective)
                with self._lock:
                    self._cache_error = str(exc)

                code, safe_summary = _classify_gemini_exception(exc)
                detail = f"{type(exc).__name__}: {exc}" if debug else None
                return _err_payload(
                    error_code=code,
                    summary=safe_summary,
                    gemini_configured=True,
                    debug=debug,
                    detail=detail,
                )

        if response is None:
            if last_exc is not None:
                _log_gemini_exception(
                    last_exc,
                    context="summarize_network_health / exhausted fallbacks",
                )
                code, safe_summary = _classify_gemini_exception(last_exc)
                detail = f"{type(last_exc).__name__}: {last_exc}" if debug else None
                return _err_payload(
                    error_code=code,
                    summary=safe_summary,
                    gemini_configured=True,
                    debug=debug,
                    detail=detail,
                )
            return _err_payload(
                error_code="model_not_found",
                summary="Gemini request did not return a response after model resolution.",
                gemini_configured=True,
                debug=debug,
                detail=resolution if debug else None,
            )

        text, issue = _extract_text_from_response(response)
        if issue == "blocked":
            logger.error(
                "Gemini blocked or filtered output (model=%s). candidates=%r",
                effective,
                getattr(response, "candidates", None),
            )
            return _err_payload(
                error_code="request_blocked",
                summary=(
                    "Gemini blocked the summary (safety/policy). "
                    "Check server logs for finish_reason / candidates."
                ),
                gemini_configured=True,
                debug=debug,
                detail="finish_reason indicates blocked or filtered content." if debug else None,
            )

        if not text:
            logger.error(
                "Gemini returned empty text after extract (model=%s). candidates=%r",
                effective,
                getattr(response, "candidates", None),
            )
            return _err_payload(
                error_code="unknown_error",
                summary=(
                    "Gemini returned no usable text. Check the Flask terminal for response details."
                ),
                gemini_configured=True,
                debug=debug,
                detail="empty_model_response" if debug else None,
            )

        generated_at = _utc_iso()
        with self._lock:
            self._cache_summary = text
            self._cache_generated_at = generated_at
            self._cache_error = None
            self._last_effective_model = effective
            self._last_model_resolution = resolution

        return {
            "summary": text,
            "generated_at": generated_at,
            "status": "success",
            "cached": False,
            "gemini_configured": True,
            "error_code": None,
            "model": effective,
            "model_resolution": resolution,
        }
