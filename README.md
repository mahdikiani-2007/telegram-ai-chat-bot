# L-Bot Modular

Persian-first Telegram AI bots with streaming chat, image understanding, task and reminder workflows, media downloaders, an admin panel, daily digests, and optional Instagram relay support.

The project is designed for Railway and Docker, uses SQLite for durable state, and keeps the historical `python bot.py` entrypoint compatible with existing deployments.

## Highlights

- L persona chat with conversation memory, summaries, rate limits, anti-spam, and group mention/reply handling.
- Optional Light persona bot (`TELEGRAM_TOKEN_LIGHT`) with chat and debate support.
- Dedicated vision routing for photo turns and `/tr` image translation.
- `/pic` image generation and `/tr` text/image translation.
- Todos, recurring reminders, snooze/dismiss actions, and Iran-time parsing.
- Instagram and YouTube link handling, quality selection, and optional large-file delivery through a Telegram userbot.
- Inline mode, chosen-inline responses, and Telegram Guest Mode support.
- Owner/admin panel for users, groups, bans, settings, jobs, broadcasts, digest modes, and digest profiles.
- Daily news digests with standard/off/custom-profile group modes, shared Tavily budget protection, delivery tracking, and retry-safe fan-out.
- Background backup, retention, WAL checkpointing, behavior profiling, and fact extraction.

## Requirements

- Python 3.13 for local development.
- `pyTelegramBotAPI==4.36.0` and the packages pinned in `requirements.txt`.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A compatible chat API key (`CHAT_API_KEY` or the legacy `AGNES_API_KEY` fallback).
- Optional credentials for image generation, web search, Instagram relay, Local Bot API, and userbot uploads.

## Quick start

```bash
git clone <repository-url>
cd <repository-directory>
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with at least:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
CHAT_API_KEY=your_chat_api_key
ADMIN_IDS=123456789
```

Run locally:

```bash
python bot.py
```

`bot.py` is a compatibility shim. It calls `main.main()`, which registers handlers, starts background workers, and runs long polling.

## Configuration

`.env.example` is the safe, public configuration template. Important settings include:

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_TOKEN` | Main L bot token (required). |
| `TELEGRAM_TOKEN_LIGHT` | Optional Light bot token. |
| `CHAT_API_KEY` / `AGNES_API_KEY` | Chat-completions authentication. |
| `CHAT_API_BASE` / `CHAT_MODEL` | Chat endpoint and model. |
| `VISION_API_KEY` / `VISION_API_BASE` / `VISION_MODEL` | Dedicated photo-understanding endpoint and model. |
| `IMAGE_API_KEY` / `AGNES_BASE` / `IMAGE_MODEL` | Image-generation endpoint and model. |
| `ADMIN_IDS` / `ADMIN_ID` | Panel admins; the first `ADMIN_IDS` value is the owner. |
| `DATA_DIR` | SQLite database, backups, downloads, and session storage. Railway deployments normally use `/data`. |
| `TAVILY_API_KEY` / `TAVILY_DAILY_LIMIT` | Optional chat and digest web-search budget. |
| `DIGEST_HOUR_IRAN` / `DIGEST_MINUTE_IRAN` | Daily digest schedule in Iran time. |
| `IG_RELAY_USERNAME` / `IG_RELAY_PASSWORD` | Optional Instagram relay account. |
| `TG_API_ID` / `TG_API_HASH` and session settings | Optional Local Bot API and large-file userbot support. |

Do not put secrets in source files, README examples, logs, issues, or commits. Runtime data is ignored by Git.

## Commands and surfaces

### Chat and media

| Command/surface | Description |
| --- | --- |
| `/start` | Show the bot introduction. |
| `/new`, `/session` | Create, list, switch, rename, or delete private chat sessions. |
| `/reset` | Reset private memory or group conversation history according to permissions. |
| `/stop` | Cancel the caller's active generation; group cancellation requires the placeholder reply. |
| `/pic <prompt>` | Generate an image. |
| `/tr <language> <text>` | Translate text. Caption a photo with `/tr` for OCR/vision translation. |
| Inline mode | Ask through inline queries after enabling `/setinline` in BotFather. |
| Guest Mode | Respond to supported mentions after enabling Telegram Guest Mode. |
| Instagram/YouTube links | Download and send supported media; YouTube offers a quality picker. |

### Tasks and reminders

| Command | Description |
| --- | --- |
| `/todo [text]` | Guided todo creation with priority, category, deadline, and recurrence. |
| `/todos` | List active and expired todos with management buttons. |
| `/remind [text]` | Guided reminder creation with relative, clock, and recurring times. |
| `/reminders` | List pending reminders and delete them when permitted. |

### Admin and operations

| Command/surface | Description |
| --- | --- |
| `/admin` | Private inline admin panel. Access is checked on every update. |
| `/stats` | Admin metrics and activity statistics. |
| `/digest` | Owner-only digest run and delivery report. |
| `/relay ...` | Owner/admin Instagram relay configuration and diagnostics. |
| `/stopall` | Admin-only stop for active L-vs-Light debates. |

Panel permissions distinguish the owner from other admins. Wide-impact and destructive actions use confirmation steps and are audited.

## Deployment

### Railway

1. Create a Railway service from this repository.
2. Add a persistent volume and set `DATA_DIR=/data`.
3. Configure the required environment variables in the Railway dashboard.
4. Deploy from the feature branch through a reviewed merge to `main`.

Railway uses `Dockerfile` and `start.sh`. The container starts the optional Local Bot API server when `TG_API_ID`/`TG_API_HASH` are configured, then executes `python3 bot.py`. Polling is long-lived; no HTTP health server is required.

### Docker

```bash
docker build -t l-bot .
docker run --env-file .env -v l-bot-data:/data l-bot
```

The image includes the media tooling used by the downloader paths. Keep the data volume persistent so the SQLite database, backups, downloads, and sessions survive restarts.

## Data and backups

The SQLite database is created at `DATA_DIR/chat_history.db` with WAL mode and thread-local connections. The bot also stores backups, media downloads, and optional Telegram/Instagram session files below `DATA_DIR`.

See [RESTORE.md](RESTORE.md) for retention, snapshot, verification, and restore procedures. Never commit `data/`, `.env`, `*.db`, cookies, or `*.session` files.

## Development and validation

Tests are hermetic: `tests/conftest.py` supplies stub credentials and a temporary data directory.

```bash
python -m pytest -q tests/
python tools/check_order.py
python -m pytest -q tests/test_dispatch.py
```

Useful diagnostics:

```bash
python tools/census.py
python tools/vision_check.py
python tools/verify_restore.py --help
```

Handler registration order is intentional because pyTelegramBotAPI dispatches first-match handlers. New commands must be registered before the catch-all chat handler and covered by the dispatch checks.

## Project map

- [PROJECT_MAP.md](PROJECT_MAP.md) — architecture and feature index.
- [docs/map/modules.md](docs/map/modules.md) — module responsibilities and public APIs.
- [docs/map/config.md](docs/map/config.md) — environment variables and message locations.
- [docs/map/integrations.md](docs/map/integrations.md) — external services and their quirks.
- [docs/map/data.md](docs/map/data.md) — database schema and runtime state.
- [RESTORE.md](RESTORE.md) — backup and restore runbook.

## Security

Treat Telegram tokens, API keys, cookies, database files, and userbot session files as secrets. Use environment variables and a persistent private volume; do not paste credentials into logs or bug reports. Review admin IDs carefully because the first configured ID is the owner.

Contributions should be made on a branch and submitted as a pull request. Do not push directly to `main`; merges deploy to Railway.
