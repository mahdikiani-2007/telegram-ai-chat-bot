# Configuration + messages/localization

## Env vars / config keys (`R` required, `O` optional; all read via `os.environ.get` in `config.py` unless noted)

| Var | R/O | Default | Purpose / notes |
|---|---|---|---|
| `TELEGRAM_TOKEN` | R | — | Main bot (L) token. Missing → `ValueError` at import |
| `TELEGRAM_TOKEN_LIGHT` | O | unset → Light bot disabled | Second bot (Kira persona); own handlers + command menu |
| `AGNES_API_KEY` | R* | — | *Required unless `CHAT_API_KEY` set. Legacy/shared LLM key |
| `CHAT_API_KEY` | R* | falls back to `AGNES_API_KEY` | Chat LLM Bearer key (header built in `infra.py`) |
| `IMAGE_API_KEY` | O | falls back to `AGNES_API_KEY` | Image-gen Bearer key |
| `ADMIN_ID` | O* | `0` (= nobody) | Legacy single admin; kept exported. Owner fallback when `ADMIN_IDS` unset. Practically required |
| `ADMIN_IDS` | O* | unset → owner = `ADMIN_ID` | Comma-separated admin IDs; first = unremovable OWNER. Invalid entries ignored + warned. No owner → panel disabled + startup warning |
| `DATA_DIR` | O | `data/` next to code (`/data` on Railway) | DB, downloads, sessions, backups, relay session |
| `LOCAL_BOT_API_URL` | O | unset → cloud API (49MB cap) | e.g. `http://localhost:8081`; raises cap to ~1950MB |
| `DROP_PENDING_UPDATES` | O | `false` | `1/true/yes` = drop Telegram queue on (re)start (one-time escape hatch) |
| `SHUTDOWN_GRACE_SECONDS` | O | `20` | In-flight drain before WAL checkpoint + exit (fit under platform SIGTERM grace) |
| `CHAT_API_BASE` | O | `https://opencode.ai/zen/v1` | Chat completions endpoint |
| `AGNES_BASE` | O | `https://apihub.agnes-ai.com/v1` | Image endpoint base (+ vision default) |
| `CHAT_MODEL` | O | `mimo-v2.5-free` | Chat model name |
| `IMAGE_MODEL` | O | `agnes-image-2.0-flash` | Image model name |
| `VISION_MODEL` | O | `agnes-3.0-flash` | Photo-understanding model — photo turns bypass the chat router (it doesn't deliver images); probe with `tools/vision_check.py` |
| `VISION_API_BASE` | O | falls back to `AGNES_BASE` | Vision completions endpoint base |
| `VISION_API_KEY` | O | falls back to `AGNES_API_KEY` | Vision Bearer key (header built in `infra.py`) |
| `CHAT_RETRY_ATTEMPTS` | O | `2` | Extra attempts *before* streaming starts |
| `RECENT_HISTORY` | O | `18` | Verbatim turns per LLM context |
| `SUMMARIZE_THRESHOLD/BATCH` | O | `60` / `40` | Compress oldest 40 when ≥60 rows |
| `STREAM_EDIT_INTERVAL` | O | `1.2` (s) | Live-edit throttle while streaming |
| `SCHEDULER_MAX_WORKERS` | O | `5` | Reminder/todo dispatch pool size |
| `MAX_DISPATCH_RETRIES` | O | `5` | Permanent-failure count before force-dismiss + admin alert |
| `SESSION_TTL_SECONDS` | O | `900` | Wizard session TTL (`sessions.py`, YT picker) |
| `SESSION_GC_INTERVAL` | O | `300` | GC sweep period |
| `ADMIN_ERROR_THROTTLE_SECONDS` | O | `60` | Per-error-type admin alert throttle |
| `DB_RETENTION_DAYS` | O | `90` | `messages` age purge (pre-backup) |
| `DB_SIZE_WARN_MB` | O | `500` (`≤0` off) | DB-size admin alert threshold |
| `BACKUP_KEEP_DAILY` | O | `7` | Local snapshot rotation count |
| `BACKUP_HOUR_IRAN` | O | `4` | Daily backup time (Iran) |
| `DIGEST_HOUR_IRAN/_MINUTE_IRAN` | O | `9` / `0` | Daily digest time (Iran) |
| `DIGEST_MEMORY_DAYS` | const | `7` | Repeat-guard window (`config.py`) |
| `TAVILY_API_KEY` | O | unset → search off | Web-search key |
| `TAVILY_SEARCH_URL` | O | Tavily endpoint | Override for proxy/mirror |
| `TAVILY_MAX_RESULTS` | O | `5` | Results per search |
| `TAVILY_DAILY_LIMIT` | O | `40` (`≤0` uncapped) | Coarse daily credit cap |
| `INSTAGRAM_COOKIES_FILE` | O | unset → anonymous | Netscape cookies.txt for IG downloads |
| `YOUTUBE_COOKIES_FILE` | O | unset → anonymous | Netscape cookies.txt for YT |
| `YOUTUBE_MAX_HEIGHT` | O | `1080` | Quality ceiling offered |
| `TG_API_ID / TG_API_HASH` | O | `0` / `""` | my.telegram.org creds: Local Bot API server *and* userbot login |
| `TG_SESSION_NAME/_DIR` | O | `yt_uploader` / `DATA_DIR` | Pyrogram session file location |
| `IG_RELAY_USERNAME/_PASSWORD` | O | unset → relay off | Dedicated IG account (`ig_relay.py`) |
| `IG_RELAY_TARGET_CHAT` | O | unset | Default Telegram target chat |
| `IG_RELAY_THREADS` | O | unset | Comma-separated IG thread ids (also settable via `/relay thread`) |
| `IG_RELAY_POLL_SECONDS` | O | `20` (min 5) | Relay poll interval |
| `IG_RELAY_MAX_ATTEMPTS` | O | `3` | Per-post relay retries |
| `IG_RELAY_LOGIN_BACKOFF` | O | `300` (min 30) | Wait after failed login/poll crash |
| `IG_RELAY_CHALLENGE_BACKOFF` | O | `900` (min 300) | Silence after IG checkpoint |
| `MAX_DEBATE_ROUNDS` | O | `50` (`services/debate.py`) | Debate turn-pair cap |
| `DEBATE_DELAY` | O | `4` (s) | Pause between debate turns |
| `DEBATE_GENERATE_TIMEOUT/ATTEMPTS` | O | `180` (s) / `2` | Per-turn LLM timeout × tries |
| `TLDR_WINDOW_MINUTES` | O | `180` (`services/memory.py`) | Default `/tldr` window |
| `TLDR_COOLDOWN_SECONDS` | O | `300` | Per-group `/tldr` cooldown |
| `FACT_EXTRACT_COOLDOWN` | O | `45` (s) | Per-user fact-extraction throttle |
| `BEHAVIOR_PROFILE_HOUR` | O | `3` (`services/memory.py`; runs at :30) | Nightly profiler start |
| `BEHAVIOR_TRANSCRIPT_MAX_CHARS` | O | `16000` | Profiler transcript cap |

Secrets live only in env/`.env` (gitignored). Never paste values into code, logs, or this map.

## Messages / localization

- **No i18n framework.** All user-facing text is inline Persian (some English admin strings) at the call site. Change text where it is sent:
  - Chat/persona voice: `services/memory.py: SYSTEM_PROMPT` (L), `SYSTEM_PROMPT_LIGHT`, debate prompts in `services/debate.py`, `/tldr` + fact-extraction + profiler prompts in `services/memory.py`, translator prompt `TRANSLATOR_SYSTEM_PROMPT` + `PERSIAN_GUIDE` in `handlers/surfaces.py`.
  - Commands: `handlers/basics.py` (`/start /reset /session /new /pic`), `handlers/tasks.py` (wizard prompts, alarm text in `services/scheduling.py: _build_reminder_alarm/_fire_todo_expiry`), `handlers/admin.py` (panel, broadcast, `/stats`, `/digest`), `handlers/memory_cmds.py` (`/tldr /myfacts /forget`), `handlers/medialink.py` (download status/fail texts), debate openers in `services/debate.py`.
- **Keyboards/buttons:** built inline next to their flow — `handlers/tasks.py: _recurrence_markup/_build_todo_list_markup/_build_inline_cancel_button`, `handlers/basics.py: _build_session_list`, medialink quality buttons in `handle_youtube_link`, snooze/dismiss in `services/scheduling.py: _build_reminder_alarm`, panel menus in `handlers/panel.py`. Callback prefixes are the contract: `todo_pri/cat/dl_none/dl_custom/dl_retry/rec/customrec/done/del`, `remind_time/ment/rec/customtime/customrec/snooze/dismiss/del`, `wait_cancel_`, `sessw_`, `yt_dl_`, `pnl_` (numeric payloads; guard re-checks instead of owner-id convention). Renaming a prefix = update handler filter + sender together.
- **Command menu:** `wiring.py: _sync_bot_commands()` (source of truth; BotFather edits get overwritten).
