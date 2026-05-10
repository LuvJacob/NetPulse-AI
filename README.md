# NetPulse AI

**NetPulse AI** is a local-first network monitoring dashboard with probe logging in SQLite, configurable targets, Discord alerts, and optional **Google Gemini** summaries of recent infrastructure health. It ships with a dark-mode multi-page UI and **Chart.js** charts for latency and uptime trends.

**Status:** Feature-complete local portfolio project.

---

## Screenshots

Place images under [`screenshots/`](screenshots/):

- `screenshots/dashboard.png` — dashboard & charts  
- `screenshots/targets.png` — target management  
- `screenshots/alerts.png` — alerts view  
- `screenshots/ai-summary.png` — AI summary panel  

(Add your own captures; filenames above match the placeholders referenced in this README. After adding files, you can embed them in this section with standard Markdown, e.g. `![Dashboard](screenshots/dashboard.png)`.)

---

## Features

- **Real-time target monitoring** — HTTP(S) probes on a configurable interval  
- **Uptime tracking** — rolling-window uptime per target  
- **Latency tracking** — response times over time with Chart.js  
- **Outage detection** — alerts when a target transitions to DOWN  
- **High latency alerts** — warns when latency crosses your threshold  
- **SQLite logging** — persistent probe samples and alerts  
- **Target management** — add, remove, enable, or disable monitored hosts  
- **Discord webhook notifications** — outages, latency alerts, and target configuration events  
- **Gemini AI network summaries** — short narrative from recent logs/alerts (optional API key)  
- **Chart.js visualizations** — latency timeline, uptime donut, outage bars  
- **Dark-mode dashboard** — cohesive SaaS-style UI  

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3, Flask |
| Database | SQLite |
| Frontend | Jinja2 templates, vanilla JS, Chart.js |
| Monitoring | `requests` HTTP probes (daemon thread) |
| Notifications | Discord Incoming Webhooks |
| AI | Google Gemini (`google-generativeai`) |

---

## Architecture overview

- **`run.py`** — launches Flask (`host=127.0.0.1`, port `5000`).  
- **`app.py`** — routes, dashboard context, REST-style JSON APIs (`/api/status`, `/api/logs`, `/api/alerts`, etc.).  
- **`config.py`** — loads `.env`, exposes `Config` for Flask and services.  
- **`database/db_manager.py`** — schema, migrations, reads/writes for targets, logs, and alerts.  
- **`services/monitor_service.py`** — background loop: probe active targets, write logs, raise SQLite alerts, trigger Discord for OUTAGE / HIGH_LATENCY.  
- **`services/notification_service.py`** — Discord delivery and cooldown for probe alerts; separate embeds for target lifecycle events.  
- **`services/ai_service.py`** — bounded context to Gemini, caching, model discovery helpers.  
- **`static/` / `templates/`** — CSS, JS, HTML pages (dashboard, alerts, targets, logs, settings).

Data flows: **monitor thread → SQLite**; **browser ↔ Flask ↔ SQLite / Gemini / Discord**.

---

## Project structure

```
NetPulse AI/
├── app.py                 # Flask app & routes
├── run.py                 # Local entrypoint
├── config.py              # Settings from environment / .env
├── requirements.txt
├── .env.example           # Template only — no secrets
├── database/
│   └── db_manager.py
├── services/
│   ├── monitor_service.py
│   ├── notification_service.py
│   └── ai_service.py
├── static/                # CSS & JS (incl. Chart.js dashboard)
├── templates/             # Jinja pages
└── screenshots/           # Optional README images (see screenshots/README.md)
```

---

## Setup

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env with your keys (see Environment variables)
python run.py
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
python run.py
```

Open **http://127.0.0.1:5000** in your browser.

---

## Environment variables

Create `.env` from `.env.example`. Common variables:

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI Studio API key for summaries (optional). |
| `GEMINI_MODEL` | Gemini model id (e.g. `models/gemini-2.5-flash` or short names like `gemini-2.0-flash` per API). |
| `DISCORD_WEBHOOK_URL` | Discord Incoming Webhook URL (optional). |
| `NETPULSE_PUBLIC_URL` | Base URL for links inside Discord embeds (e.g. `http://127.0.0.1:5000`). |
| `MONITOR_INTERVAL` | Seconds between probe rounds (default `30`). |
| `LATENCY_ALERT_MS` | Latency threshold for HIGH_LATENCY alerts (default `300`). |
| `NOTIFICATIONS_ENABLED` | `true` / `false` — master switch for Discord. |
| `NOTIFICATION_COOLDOWN_MINUTES` | Cooldown between duplicate Discord alerts per target + alert type. |
| `NETPULSE_DB` | SQLite filename relative to project root (default `netpulse.sqlite3`). |

Optional Flask: `SECRET_KEY` (defaults to a dev value — override in any shared deployment).

---

## Run locally

```bash
python run.py
```

The app listens on **127.0.0.1:5000**. The monitor thread starts automatically.

---

## Test Gemini

1. Set `GEMINI_API_KEY` in `.env`.  
2. Restart the app.  
3. Open **http://127.0.0.1:5000/api/test-gemini** — expect JSON `status: success` or a clear error (quota, model name, etc.).  
4. On the dashboard, use **Refresh AI summary** or **GET /api/ai-summary** (needs some probe data in SQLite first).

---

## Test Discord

1. Create an Incoming Webhook in Discord (Server Settings → Integrations → Webhooks).  
2. Set `DISCORD_WEBHOOK_URL` in `.env`. Restart the app.  
3. **Ping:** **GET** `http://127.0.0.1:5000/api/test-discord`  
4. **Target embed sample:** **GET** `http://127.0.0.1:5000/api/test-discord?type=target`  

Responses never include the webhook URL.

---

## Safety & secrets

- **Never commit `.env`.** It is listed in `.gitignore`.  
- If secrets were ever committed, **rotate** them (new Gemini key, new Discord webhook) and scrub git history if needed.  
- Use **`.env.example`** only for documentation — placeholders only, no real keys or tokens.  
- **Do not commit** `*.sqlite3` / `*.db` — they contain local telemetry.

---

## Common troubleshooting

| Issue | What to try |
|-------|-------------|
| Gemini errors / model not found | Call `/api/list-gemini-models`, pick an allowed model, set `GEMINI_MODEL` accordingly. |
| Discord not firing | Check `DISCORD_WEBHOOK_URL`, `NOTIFICATIONS_ENABLED=true`, and Flask terminal for HTTP errors. |
| Targets show wrong enabled state | Restart after DB migrations; ensure only one `python run.py` instance binds port 5000. |
| Empty AI summary | Wait for monitoring samples; summaries need recent logs in SQLite. |
| Port already in use | Stop other apps on port 5000 or adjust Flask `run.py` / hosting later. |

---

## Resume bullet

Built **NetPulse AI**, an AI-assisted network monitoring dashboard using **Python**, **Flask**, **SQLite**, **Chart.js**, **Discord Webhooks**, and **Google Gemini API** to monitor uptime, latency, outages, configurable targets, and generate automated infrastructure health summaries.

---

## Future improvements

- Container image & compose file for repeatable installs  
- Hosted deployment (Render, Railway, etc.) with `PORT` / `0.0.0.0`  
- Email / Slack connectors  
- Auth for multi-user or public demos  
- Retention policies and DB backups  

---

## License

No license file is included by default; add one if you open-source the repo publicly.
