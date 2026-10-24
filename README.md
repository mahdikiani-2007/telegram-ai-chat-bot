# Telegram AI Chat Bot

Feature-rich, Persian-first Telegram AI bot with streaming chat, durable memory, vision, image tools, tasks, reminders, media workflows, admin controls, daily digests, and Railway/Docker deployment.

The repository contains the current modular implementation. Bot **L** is the primary detective persona; an optional **Light** bot provides a second persona for chat and debates. The service uses long polling and SQLite/WAL persistence rather than a web server.

## Contents

- [Capabilities](#capabilities)
- [Architecture and project layout](#architecture-and-project-layout)
- [Requirements](#requirements)
- [Local installation](#local-installation)
- [Environment variables](#environment-variables)
- [Running the bot](#running-the-bot)
- [Commands and workflows](#commands-and-workflows)
- [Persistence and data safety](#persistence-and-data-safety)
- [Docker deployment](#docker-deployment)
- [Railway deployment](#railway-deployment)
- [Administration and security](#administration-and-security)
- [Testing and validation](#testing-and-validation)
- [Backup and restore](#backup-and-restore)
- [Troubleshooting](#troubleshooting)
- [Deployment notes](#deployment-notes)
- [License](#license)

## Capabilities

### AI chat and memory

- Streaming responses for private chats and group messages that mention, reply to, or call L.
- Conversation history and rolling summaries stored in SQLite.
- Per-user private sessions with create, switch, rename, and delete support.
- Long-term user facts and behavior profiles, plus group `/tldr` summaries.
- Optional Tavily-backed web search with a shared daily budget for chat and digest work.
- Rate limiting, burst anti-spam protection, cancellation with `/stop`, and throttled admin error notifications.

### Vision, translation, and image generation

- Dedicated vision routing for photo conversations; photo turns do not rely on the text-only chat router.
- `/tr` translates text and can OCR/translate a photo captioned with `/tr`.
- `/pic` generates an image through the configured OpenAI-compatible image endpoint.

### Tasks, reminders, and debates

- Guided todos with priority, category, deadlines, delegation, and recurrence.
- Reminders with relative times, Iran-clock times, recurrence, mention targets, snooze, and dismiss.
- Scripted L-vs-Light debates with `/stopall` and configurable round/time limits.

### Media and Telegram surfaces

- Instagram public-post/reel downloading through yt-dlp, with optional browser cookies.
- YouTube quality selection, ffmpeg post-processing, optional cookies, and Deno for yt-dlp JavaScript challenges.
- Optional Pyrogram userbot delivery for files larger than the cloud Bot API upload limit.
- Inline mode, chosen-inline responses, and Telegram Guest Mode.
- Optional Instagram direct-message relay to a Telegram chat.

### Operations

- Private inline admin panel for users, groups, bans, settings, jobs, broadcasts, digest modes, and digest profiles.
- Daily digest scheduler with standard, off, and custom-profile group modes, delivery tracking, retry-safe fan-out, and Tavily budget protection.
- Daily SQLite snapshot, retention maintenance, WAL checkpointing, session cleanup, fact extraction, and behavior profiling.

## Architecture and project layout

The application is deliberately split into small modules while retaining `bot.py` as the deployment-compatible entrypoint:

```text
bot.py                  compatibility shim; calls main.main()
main.py                 startup, diagnostics, worker threads, polling threads
wiring.py               handler order, command menu, polling, shutdown
config.py               environment parsing, defaults, paths, logging
bots.py                 Telegram bot instances and admin notifications
db.py                   SQLite repository and schema migrations
ai.py                   chat, summary, vision, and image API clients
sessions.py             TTL wizard/session state and garbage collection
limits.py               rate limiter and anti-spam tracker
ig_relay.py             Instagram relay service and `/relay` command
handlers/               Telegram commands, callbacks, panels, and chat pipelines
services/               memory, streaming, media, digest, scheduling, backup, search
tools/                  developer diagnostics and restore verification
tests/                  hermetic regression and integration-style tests
```

`wiring.register_all()` is the dispatch contract. pyTelegramBotAPI uses first-match handler dispatch, so the project registers guards, admin/panel flows, task wizards, commands, media handlers, memory commands, and finally the catch-all chat handlers in a deliberate order.

### Startup flow

1. `start.sh` optionally starts the Local Bot API Server on port `8081`.
2. `start.sh` executes `python3 bot.py`; `bot.py` calls `main.main()`.
3. `main()` installs signal handlers, registers all Telegram handlers, logs dependency diagnostics, deletes any webhook, and synchronizes command menus.
4. Background workers start for reminders, session cleanup, digests, backups, fact extraction, behavior profiling, userbot delivery, and Instagram relay.
5. L polling starts, and Light polling starts when `TELEGRAM_TOKEN_LIGHT` is configured.
6. Shutdown stops polling, allows in-flight work to finish within the grace period, joins polling threads, and checkpoints SQLite WAL.

Updates are received through `infinity_polling` with message, edited-message, callback, inline, chosen-inline, and Guest Mode update types. The webhook is deleted on startup; pending updates are preserved unless `DROP_PENDING_UPDATES` is explicitly enabled.

## Requirements

- Python **3.13** for local execution.
- `pyTelegramBotAPI==4.36.0`.
- The exact pinned packages in [`requirements.txt`](requirements.txt).
- A Telegram bot token created with [@BotFather](https://t.me/BotFather).
- A chat API key: `CHAT_API_KEY`, or the legacy `AGNES_API_KEY` fallback.
- Optional keys for image generation, vision, Tavily search, Instagram relay, and userbot features.
- Docker for the production image; Railway supplies the hosted runtime and persistent volume.

The Docker image also builds the Local Bot API Server and includes ffmpeg and a pinned Deno binary for media workflows.

## Local installation

```bash
git clone https://github.com/mahdikiani-2007/telegram-ai-chat-bot.git
cd telegram-ai-chat-bot

python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env` with real values. At minimum:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
CHAT_API_KEY=your_chat_api_key
ADMIN_IDS=123456789
```

Never commit `.env`, tokens, API keys, browser cookies, database files, or Telegram session files. `.env.example` contains names and safe defaults only.

## Environment variables

All variables are read from the process environment. Start with [`.env.example`](.env.example); it is the public template and intentionally contains no credentials. Runtime panel settings are stored in SQLite, not in `.env`.

### Required and identity

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | none | Main L bot token; required. |
| `AGNES_API_KEY` | none | Legacy/shared AI key; used as fallback when specific keys are absent. |
| `CHAT_API_KEY` | `AGNES_API_KEY` | Chat-completions bearer key; one of this or `AGNES_API_KEY` is required. |
| `ADMIN_IDS` | unset | Comma-separated positive Telegram IDs. The first ID is the owner. |
| `ADMIN_ID` | `0` | Legacy single-admin fallback. |
| `TELEGRAM_TOKEN_LIGHT` | unset | Enables the optional Light bot when set. |

### AI endpoints and behavior

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHAT_API_BASE` | `https://opencode.ai/zen/v1` | OpenAI-compatible chat endpoint. |
| `CHAT_MODEL` | `mimo-v2.5-free` | Chat model name. |
| `IMAGE_API_KEY` | `AGNES_API_KEY` | Image-generation key. |
| `AGNES_BASE` | `https://apihub.agnes-ai.com/v1` | Image endpoint base and vision fallback base. |
| `IMAGE_MODEL` | `agnes-image-2.0-flash` | Image-generation model. |
| `VISION_API_KEY` | `AGNES_API_KEY` | Dedicated photo-understanding key. |
| `VISION_API_BASE` | `AGNES_BASE` | Dedicated vision endpoint base. |
| `VISION_MODEL` | `agnes-3.0-flash` | Photo-understanding model. |
| `CHAT_RETRY_ATTEMPTS` | `2` | Extra attempts before content starts streaming. |
| `RECENT_HISTORY` | `18` | Verbatim recent message rows included in context. |
| `SUMMARIZE_THRESHOLD` | `60` | Message rows that trigger compression. |
| `SUMMARIZE_BATCH` | `40` | Oldest rows compressed per summary pass. |
| `STREAM_EDIT_INTERVAL` | `1.2` | Minimum seconds between streaming message edits. |

### Storage, scheduling, and shutdown

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `data/` beside the code, `/data` in Docker | SQLite, backups, downloads, Local API files, and sessions. |
| `SCHEDULER_MAX_WORKERS` | `5` | Reminder/todo dispatch worker count. |
| `MAX_DISPATCH_RETRIES` | `5` | Permanent dispatch failures before force-dismiss and alert. |
| `SESSION_TTL_SECONDS` | `900` | TTL for wizard state and temporary selections. |
| `SESSION_GC_INTERVAL` | `300` | Session/data garbage-collection interval. |
| `ADMIN_ERROR_THROTTLE_SECONDS` | `60` | Per-error-type admin alert throttle. |
| `DROP_PENDING_UPDATES` | `false` | Set to `true` only as a one-time backlog escape hatch. |
| `SHUTDOWN_GRACE_SECONDS` | `20` | Grace period for in-flight work before shutdown/WAL checkpoint. |
| `DB_RETENTION_DAYS` | `90` | Message history retention; summaries and durable records are kept. |
| `DB_SIZE_WARN_MB` | `500` | Admin warning threshold for database size; `0` or less disables it. |
| `BACKUP_KEEP_DAILY` | `7` | Number of local daily snapshots retained. |

### Search and daily jobs

| Variable | Default | Purpose |
| --- | --- | --- |
| `TAVILY_API_KEY` | unset | Enables Tavily web search; absent means search is off. |
| `TAVILY_SEARCH_URL` | `https://api.tavily.com/search` | Tavily-compatible search endpoint. |
| `TAVILY_MAX_RESULTS` | `5` | Results requested per search. |
| `TAVILY_DAILY_LIMIT` | `40` | Shared daily search budget; `0` or less means uncapped. |
| `DIGEST_HOUR_IRAN` | `9` | Daily digest hour in Iran time. |
| `DIGEST_MINUTE_IRAN` | `0` | Daily digest minute in Iran time. |
| `BACKUP_HOUR_IRAN` | `4` | Daily backup hour in Iran time. |
| `MAX_DEBATE_ROUNDS` | `50` | Maximum scripted debate rounds. |
| `DEBATE_DELAY` | `4` | Seconds between debate turns. |
| `DEBATE_GENERATE_TIMEOUT` | `180` | Per-turn debate generation timeout. |
| `DEBATE_GENERATE_ATTEMPTS` | `2` | Per-turn debate generation attempts. |
| `TLDR_WINDOW_MINUTES` | `180` | Default group `/tldr` window. |
| `TLDR_COOLDOWN_SECONDS` | `300` | Per-group `/tldr` cooldown. |
| `FACT_EXTRACT_COOLDOWN` | `45` | Per-user fact extraction cooldown. |
| `BEHAVIOR_PROFILE_HOUR` | `3` | Iran-time hour for behavior profiling; runs around `:30`. |
| `BEHAVIOR_TRANSCRIPT_MAX_CHARS` | `16000` | Maximum profiler transcript size. |

### Media, Local Bot API, and userbot

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOCAL_BOT_API_URL` | unset | Redirects Bot API calls to a Local Bot API Server; enables the larger upload limit. |
| `INSTAGRAM_COOKIES_FILE` | unset | Netscape cookies file for more reliable Instagram downloads. |
| `YOUTUBE_COOKIES_FILE` | unset | Netscape cookies file for more reliable YouTube extraction. |
| `YOUTUBE_MAX_HEIGHT` | `1080` | Maximum offered YouTube video height. |
| `TG_API_ID` | `0` | Telegram application ID for Local Bot API/userbot. |
| `TG_API_HASH` | empty | Telegram application hash. |
| `TG_SESSION_NAME` | `yt_uploader` | Pyrogram session name. |
| `TG_SESSION_DIR` | `DATA_DIR` | Pyrogram session directory. |

Use a dedicated browser account when exporting cookies. Use a dedicated Telegram account for the optional userbot, complete its first login interactively, and protect the resulting `.session` file.

### Instagram relay

| Variable | Default | Purpose |
| --- | --- | --- |
| `IG_RELAY_USERNAME` | empty | Dedicated Instagram relay account username. |
| `IG_RELAY_PASSWORD` | empty | Dedicated Instagram relay account password. |
| `IG_RELAY_TARGET_CHAT` | empty | Default Telegram destination chat. |
| `IG_RELAY_THREADS` | empty | Comma-separated Instagram thread IDs. |
| `IG_RELAY_POLL_SECONDS` | `20` (minimum `5`) | Relay polling interval. |
| `IG_RELAY_MAX_ATTEMPTS` | `3` (minimum `1`) | Per-post retry limit. |
| `IG_RELAY_LOGIN_BACKOFF` | `300` (minimum `30`) | Delay after login/poll failure. |
| `IG_RELAY_CHALLENGE_BACKOFF` | `900` (minimum `300`) | Delay after an Instagram checkpoint/challenge. |

## Running the bot

### Local polling

With the virtual environment active and `.env` loaded by your process manager or shell:

```bash
set -a
. ./.env
set +a
python bot.py
```

The process runs long polling in foreground-managed threads. It does not open an HTTP health endpoint. Stop it with `SIGTERM` or `Ctrl-C`; the shutdown path stops polling and checkpoints WAL.

### Direct module entry

`python bot.py` is the supported compatibility command. `bot.py` contains no application logic; it imports and runs `main.main()`. Do not run handler modules individually.

## Commands and workflows

### Chat and sessions

| Command/surface | Behavior |
| --- | --- |
| `/start` | Introduces L and provides session hints. |
| `/new` | Creates a fresh private conversation session. |
| `/session` | Lists sessions; supports switching, renaming, and deleting private sessions. |
| `/stop` | Cancels the caller's active response; group cancellation requires replying to the placeholder and appropriate permission. |
| `/reset` | Resets private memory or group message history according to permission rules. |
| Plain text/photo | Private text is handled directly; group text must mention/reply to/call L. Photos use the dedicated vision client. |

### Translation and images

| Command | Behavior |
| --- | --- |
| `/tr [language] [text]` | Translates text; when sent as a photo caption, uses vision/OCR. Empty input returns the built-in Persian guidance. |
| `/pic <prompt>` | Generates an image and sends the result. |

### Tasks and reminders

| Command | Behavior |
| --- | --- |
| `/todo [text]` | Guided priority/category/deadline/recurrence flow; supports delegation with `@username`. |
| `/todos` | Lists active and expired todos with done/delete controls. |
| `/remind [text]` | Guided time, mention-target, and recurrence flow. |
| `/reminders` | Lists pending reminders; permitted users can delete them. |

Time input supports relative values, Iran-clock times, tomorrow-at-9, and custom recurrence formats handled by `timeutil.py`.

### Memory and summaries

| Command | Behavior |
| --- | --- |
| `/tldr [N]` | Group-only summary of recent chat history, subject to the group cooldown. |
| `/myfacts` | Lists durable facts learned for the current scope. |
| `/forget <number\|all>` | Deletes one fact or all facts. |

### Media and Telegram surfaces

- Send an Instagram or YouTube URL to invoke the matching downloader.
- YouTube presents quality choices; ffmpeg and Deno improve availability and merging.
- Enable `/setinline` in BotFather for inline queries and `/setinlinefeedback 100%` for chosen-inline result feedback.
- Enable Telegram Guest Mode per bot to receive `guest_message` updates.

### Admin and operations

| Command | Access and behavior |
| --- | --- |
| `/admin` | Private inline panel. Every command, callback, and typed input is permission-checked again. |
| `/stats` | Admin-only metrics and system statistics. |
| `/digest` | Owner-only on-demand digest run and delivery report. |
| `/relay ...` | Admin relay setup/status/login/logout/target/thread controls. |
| `/stopall` | Admin-only cancellation of active debates. |

The panel also manages known users/groups, bans, feature toggles, settings, rate-limit overrides, jobs, broadcasts, digest group modes, and digest profiles. Destructive or wide-impact actions use confirmation steps and write audit records.

## Persistence and data safety

SQLite is stored at `DATA_DIR/chat_history.db` and uses WAL mode, thread-local connections, and serialized writes. Runtime directories include:

```text
DATA_DIR/
├── chat_history.db
├── backups/
├── ig_downloads/
├── yt_downloads/
├── botapi/                 # Local Bot API files/logs when enabled
└── <Telegram session files when userbot is enabled>
```

The database contains conversations, summaries, todos, reminders, sessions, known chats/members, facts, behavior data, digest history/deliveries, panel settings/admins/bans/audit, broadcasts, and relay state. Message retention applies to `messages`; durable summaries, facts, tasks, reminders, sessions, and profiles are not purged by that policy.

Attach a persistent volume in production. Without one, redeploys can lose SQLite state, local snapshots, downloaded media, and optional session files.

## Docker deployment

Build and run:

```bash
docker build -t telegram-ai-chat-bot .
docker run --env-file .env -v telegram-ai-chat-bot-data:/data telegram-ai-chat-bot
```

The multi-stage Dockerfile:

- Builds a pinned Local Bot API Server from source.
- Uses `python:3.13-slim` for runtime.
- Installs ffmpeg for YouTube post-processing.
- Installs a pinned, checksum-verified Deno binary for yt-dlp JavaScript challenges.
- Installs the exact Python dependencies from `requirements.txt`.
- Sets `DATA_DIR=/data` and starts `./start.sh`.

If `TG_API_ID` and `TG_API_HASH` are unset, `start.sh` skips the Local Bot API Server and uses Telegram's cloud API with its smaller upload limit. If set, `start.sh` starts the local server, waits up to 30 seconds for port `8081`, and then runs the bot.

## Railway deployment

1. Create or connect a Railway service to this repository.
2. Configure a persistent Railway Volume mounted at `/data`.
3. Add required and optional variables in the Railway Variables panel; use [`.env.example`](.env.example) as the name/default reference.
4. Deploy the reviewed branch through `main`.

Railway builds the Dockerfile and runs `start.sh`. The service is a long-polling worker, not an HTTP web service; do not add a health server requirement that is not present in the code. `DATA_DIR=/data` is already set by the image. Keep Telegram, AI, cookie, and session credentials in Railway Variables, never in the repository.

Merges to `main` are production deployments. Review changes on a branch and use a pull request.

## Administration and security

- The first valid ID in `ADMIN_IDS` is the owner; `ADMIN_ID` is a legacy fallback.
- Owner-only actions include broadcasts, reset-user-data, leaving chats, admin membership changes, log/database exports, job run-now/pause, digest profiles, and global digest runs.
- Other admins can use permitted user/group/ban/toggle/settings/statistics and per-group digest controls.
- The panel is private-chat-only and silently denies strangers or groups.
- Admins cannot be banned or reset; users cannot ban/reset themselves.
- Panel mutations are audited; destructive actions require confirmation.
- Ban and maintenance gates protect both L and Light update streams.
- Do not log or commit tokens, API keys, passwords, cookies, `.db`, or `.session` files.
- Instagram and YouTube cookies should belong to dedicated accounts, not personal accounts.
- Treat Telegram userbot sessions as full account credentials.

## Testing and validation

The test suite is hermetic. `tests/conftest.py` supplies stub credentials and a temporary data directory, so tests do not use production tokens or databases.

Run the required checks from the repository root:

```bash
python -m pytest -q tests/
python tools/check_order.py
python -m pytest -q tests/test_dispatch.py
```

Additional diagnostics:

```bash
python tools/census.py
python tools/vision_check.py
python tools/verify_restore.py --help
```

`tools/vision_check.py` is a live endpoint probe and requires real vision configuration; it is not part of the hermetic test suite. `python tools/check_order.py` protects the first-match registration order.

## Backup and restore

The backup scheduler runs around `BACKUP_HOUR_IRAN` (default 04:00 Iran time). It purges old message rows according to `DB_RETENTION_DAYS`, periodically vacuums, creates a gzip SQLite snapshot in `DATA_DIR/backups/`, rotates local snapshots according to `BACKUP_KEEP_DAILY`, and sends a copy to the configured owner/admin.

Verify an archive before trusting it:

```bash
python tools/verify_restore.py \
  --archive DATA_DIR/backups/chat_history_YYYY-MM-DD_HHMM.db.gz
```

To restore safely:

1. Stop the bot; never replace the database while it is running.
2. Verify the archive.
3. Restore to the mounted data directory:

   ```bash
   python tools/verify_restore.py \
     --archive DATA_DIR/backups/<archive>.db.gz \
     --restore /data/chat_history.db
   ```

4. Restart and inspect the first minute of logs. Startup migrations create missing columns/indexes when needed.

See [`RESTORE.md`](RESTORE.md) for the full runbook and retention settings. `tests/test_backup.py` covers snapshot creation, pruning, verification, and restore round trips.

## Troubleshooting

### The bot does not start

- Confirm `TELEGRAM_TOKEN` and `CHAT_API_KEY`/`AGNES_API_KEY` are set.
- Confirm the process is using Python 3.13 and `pip install -r requirements.txt` was run in that same environment.
- Check that `DATA_DIR` is writable.

### Guest Mode or inline mode does not respond

- Verify `pyTelegramBotAPI==4.36.0` is installed in the running environment.
- Enable `/setinline` and `/setinlinefeedback 100%` in BotFather for inline behavior.
- Enable Guest Mode for the specific bot.
- Review startup diagnostics for guest-handler registration.

### Images are not understood

- Photo turns use `VISION_API_BASE`, `VISION_API_KEY`, and `VISION_MODEL`, not only the chat router.
- Confirm the vision key falls back as expected and run `python tools/vision_check.py` with real credentials.

### Instagram or YouTube downloads fail

- Cloud IPs are frequently rate-limited. Configure dedicated Netscape cookies files.
- Ensure `deno` is available for YouTube EJS challenges; the Docker image includes it.
- Ensure ffmpeg is available when a selected YouTube format needs merging.
- Check available disk space under `DATA_DIR`.

### Large files cannot be sent

- Telegram's cloud Bot API has a smaller upload limit. Configure `TG_API_ID`/`TG_API_HASH` and `LOCAL_BOT_API_URL` for the Local Bot API path, or configure the Pyrogram userbot for large-file delivery.
- A Pyrogram session must be created interactively first and placed in `TG_SESSION_DIR`.

### Old messages appear after deploy

This is normally intentional: `DROP_PENDING_UPDATES` defaults to `false`, preserving Telegram's server-side queue. Set it to `true` only once to discard a stale backlog, then remove the setting.

### Railway data disappeared

Check that a persistent Railway Volume is mounted at `/data`. Restore the latest verified archive using [`RESTORE.md`](RESTORE.md).

## Deployment notes

- `main` is the production deployment branch; use pull requests.
- Do not force-push, delete release tags, or replace persistent data files during an incident.
- Keep `DROP_PENDING_UPDATES=false` during normal redeploys.
- Keep a persistent volume and verify backups off-site.
- Review `PROJECT_MAP.md` and the linked `docs/map/` files before changing handlers, schema, configuration, or integrations.
- The command menu is synchronized by `wiring.py`; manual BotFather command edits are overwritten at startup.

## License

This project is distributed under the [MIT License](LICENSE). Copyright © 2026 Moai (`@moai_artwork`). See [`LICENSE`](LICENSE) for the full text.
