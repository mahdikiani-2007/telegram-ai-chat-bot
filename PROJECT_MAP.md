> This file is the authoritative map of the codebase. Read it BEFORE exploring code. Keep it updated after EVERY code change.

`Last verified:` 2026-10-06 — digest topics Stage A (per-group modes, profiles backend, ledger, budget gate).

Detail files (read when the summary below is not enough): `docs/map/modules.md` (per-file API), `docs/map/data.md` (tables + session/FSM state), `docs/map/integrations.md` (external APIs + quirks), `docs/map/config.md` (every env var + message/keyboard locations).

## 1. Overview

Dual-persona Telegram AI bot (Persian-first). Main bot **L** (detective persona): streaming chat + vision, image gen (`/pic`), translation (`/tr`), todos/reminders with schedulers, Instagram/YouTube downloaders, daily news digest, scripted L-vs-Light debates, per-user long-term memory (facts + behavior profiles), group summaries (`/tldr`), admin broadcast panel. Optional second bot **Light** (Kira persona, `TELEGRAM_TOKEN_LIGHT`): chat + debates only.
Language/runtime: **Python 3.13** (Docker `python:3.13-slim`). Telegram lib: **pyTelegramBotAPI 4.36.0** (Guest Mode API required). Runs on **long-polling** (`infinity_polling`; webhook deleted on start). Deployed on Railway: `Dockerfile` → `./start.sh` → **`exec python3 bot.py`** (`bot.py` is a shim; real entry `main.py: main()`).

## 2. Directory tree

- `bot.py` — entry shim, calls `main.main()` (kept for start.sh/Docker CMD).
- `main.py` — entrypoint: signals, `register_all()`, diagnostics, daemon threads, polling threads, shutdown wait.
- `wiring.py` — handler registration order, polling loops, `/`-menu sync, shutdown sequence.
- `config.py` — all env/config, logging setup, `DATA_DIR`/`DB_FILE`, Iran tz. Import creates dirs.
- `bots.py` — `bot` + `bot_light` instances, lazy identity cache, throttled `admin_notifier`.
- `infra.py` — shared HTTP session, telebot apihelper patch (middleware on, Local API URLs), LLM headers.
- `db.py` — SQLite `ChatRepository` (`repo`) + broadcast helpers. Only data layer.
- `ai.py` — `agnes` LLM client (stream/generate/summarize/image).
- `sessions.py` — TTL wizard-session stores + GC loop.
- `limits.py` — `rate_limiter` (per-cat sliding window) + `spam_tracker` (mute logic).
- `metrics.py` — `METRICS` counters + `_START_TIME` (for `/stats`).
- `timeutil.py` — Iran-time helpers, user time/recurrence parsing.
- `ig_relay.py` — standalone IG-DM → Telegram relay (own polls, `/relay` command, own DB tables).
- `handlers/` — `shared.py` (helpers incl. `is_admin/is_owner`, no decorators); `guard.py` (foreign-cmd swallow + tracking middleware); `admin.py` (`/stats`, owner-only `/digest`); `panel.py` (Stage 1: `/admin` inline panel — Status, Maintenance, Admins, Audit — + `pnl_` callbacks + typed-input interceptor; Stage 3A: broadcast flow); `tasks.py` (`/todo`, `/remind`, `/todos`, wizards, all callbacks); `basics.py` (`/start /session /new /stop /reset /pic`); `surfaces.py` (inline, guest mode, `/tr`); `debate.py` (`/stopall`); `medialink.py` (IG/YT links); `memory_cmds.py` (`/tldr /myfacts /forget`); `chat.py` (catch-all pipelines L + Light).
- `services/` — `panel_settings.py` (Stage 1: DB settings + 10s cache, `feature_enabled`, maintenance/ban reads, `check_panel_access`, audit, central-gate installer); `memory.py` (personas, context, summaries, facts, profiler, tldr consts); `search.py` (Tavily); `streaming.py` (LLM→Telegram streaming, `/stop` registry); `media.py` (IG/YT downloaders, userbot sender); `debate.py` (debate engine); `digest.py` (news digest); `scheduling.py` (reminder/todo dispatcher); `backup.py` (retention + daily snapshot).
- `tools/` — dev utilities, never imported by bot: `census.py` (static inventory), `check_order.py` + `dump_dispatch.py` + `dispatch_diff.py` (dispatch-order guards), `extract.py` (historical refactor tool), `shutdown_probe.py` (SIGTERM harness), `verify_restore.py` (snapshot verify/restore).
- `tests/` — `conftest.py` (hermetic stub env, must stay first-imported); `test_smoke/dispatch/backup/media_youtube/pending_updates/shutdown/panel.py`; `baseline_inventory.json` (census baseline).
- `.env.example` — documented env template (copy to gitignored `.env`).
- `start.sh` — container entry: boots Local Bot API server if `TG_API_ID/HASH` set (waits ≤30s), then `exec python3 bot.py`.
- `Dockerfile` — 2-stage: builds `telegram-bot-api` from pinned source + deno + ffmpeg + `requirements.txt`.
- `requirements.txt` — pinned: `pyTelegramBotAPI==4.36.0 requests urllib3 yt-dlp imageio-ffmpeg instagrapi`.
- `RESTORE.md` — backup/restore runbook. `.env` — secrets (gitignored, never commit). `.gitignore` — `__pycache__ .env .pytest_cache data/`.
- `data/` — runtime only (gitignored): `chat_history.db`, `ig/yt_downloads/`, `backups/`, `botapi/`, session files.
- `PROJECT_MAP.md` + `docs/map/` — this index + detail files.
- `AGENTS.md` — agent rules: read the map first, run the update skill after changes.
- `.opencode/skills/update-project-map/SKILL.md` — skill that keeps this map in sync after code changes.

## 3. Entry point and startup flow (`main.py: main()`)

1. `start.sh`: optional Local Bot API server on `:8081` → `exec python3 bot.py` → `main()`.
2. `main()` installs SIGTERM/SIGINT → `_request_shutdown`, calls `wiring.register_all()`, logs version/guest-support/cookies/deno diagnostics.
3. `_ensure_polling_mode(bot)` = `delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)`; `time.sleep(1)`; `_sync_bot_commands()` pushes `/` menus; warms `get_bot_identity()` (+ Light).
4. Starts daemon threads: `_reminder_scheduler_loop`, `_session_gc_loop`, `_digest_scheduler_loop`, `_backup_scheduler_loop`, `fact_extractor.run_forever`, `_behavior_profiler_loop`, `_userbot_thread_main`; then `ig_relay.start()`.
5. Starts polling threads `run_bot_l` (+ `run_bot_light` if configured); each loop = ensure-polling + `infinity_polling(allowed_updates=POLLING_ALLOWED_UPDATES)` with crash-restart + admin notify. Main thread waits on `shutdown_event` → `_shutdown_sequence()` (stop polling, grace wait, join, WAL checkpoint).

## 4. Request/update flow

Telebot **first-match-wins**; `wiring.register_all()` fixes the order: `handlers.guard` (swallow `/cmd@OtherBot`, then `/relay`) → `handlers.admin` → `handlers.panel` (panel `pnl_` callbacks + typed-input interceptor, before task wizards) → `handlers.tasks` → `handlers.basics` → `handlers.surfaces` (inline/chosen/guest registered at import; `/tr`) → `handlers.debate` (`/stopall`) → `handlers.medialink` (IG/YT link funcs) → `handlers/memory_cmds` (`/tldr /myfacts /forget`) → `handlers.chat` catch-all **last**. **Central gate (Stage 1):** both instances' `process_new_updates` is wrapped at startup — banned senders dropped silently, non-admins held with a rate-limited notice during maintenance (admins exempt); dropped updates skip the tracking middleware below. Middleware (`guard._track_chat_middleware`, both bots) upserts `known_chats`/`group_members` + behavior signals + `chat_log` on every message first. Slash-commands route by `commands=[...]`; multi-step wizards (todo/remind/broadcast/custom-time) intercept via TTL-session funcs registered *ahead* of the catch-all; inline taps arrive as `chosen_inline_result`; guest mentions as `guest_message` (answered synchronously via `answer_guest_query`). Chat pipeline (`handle_all_messages`, Light mirror): leading-`/` skip → `should_respond` (DM always; groups need mention/reply/name-call `L`/`ال`) → bot-media-reply skip → debate trigger/guard → name-call strip/reply → spam check → rate limit → mention strip → private-session resolve → fact-queue → photo download→base64 → `save_message(user)` → placeholder → web-search (status edit) → `build_context` + profile inject → `_register_active_request` → `stream_to_telegram` (live edits) → stop/error/empty branches (memory cleanup on stop/error) → `finalize_message` → `save_message(assistant)` → `maybe_summarize` → `METRICS handled+1`. Any exception → log + throttled admin notify + user-facing fallback.

## 5. Feature/handler index

`bot` = main L bot. All callback buttons owner-gated (only the invoker clicks, except todo done/del = creator-or-admin, snooze/dismiss = any same-chat member with chat-match IDOR guard).

| Feature / Command / Callback | Trigger | File | Function/Class | Notes |
|---|---|---|---|---|
| Swallow foreign-bot commands | func `_is_command_for_another_bot` (`/cmd@OtherBot`) | `handlers/guard.py` | `_ignore_foreign_bot_commands[_light]` | Must stay first; relies on first-match dispatch |
| Chat/member tracking middleware | `middleware_handler(update_types=["message"])` | `handlers/guard.py` | `_track_chat_middleware[_light]` | Writes `known_chats`, `group_members`, `behavior_stats`, `chat_log` |
| `/relay` (+ `help/status/on/off/target/threads/thread/test/verify/resume`) | `commands=[relay]`, admin-only | `ig_relay.py` | `_handle_relay_command` → `handle_command` | Own `ig_relay_config/seen` tables; poll thread |
| `/admin` panel home (+ `pnl_home/status/maint(_cf/_apply)/admins/add/del/del_ok/cancel/audit/bcast`) | `commands=[admin]` + `pnl_` callbacks, any-admin (Admins + Broadcast buttons owner-only), private-only, silent otherwise | `handlers/panel.py` | `handle_admin_panel`, `panel_guard`, `check_panel_access` (in `services/panel_settings.py`) | Single message edited in place; typed admin-ID input via `_panel_input_sessions`; mutating actions audited |
| Users (`pnl_users_*/user_*/upick_*/user_search`) | callbacks + typed search, any-admin, private-only | `handlers/panel.py` | `handle_panel_users/user/pick`, `handle_panel_user_search` | 10/page, counts/metadata only, pick-from-results (never first-match); Ban/Unban confirm-gated, admins/self protected |
| Groups (`pnl_groups_*/group_*/gpick_*/group_search`) + Leave (owner-only) + per-group debate toggle + digest mode switch | callbacks + typed search | `handlers/panel.py` | `handle_panel_groups/group/pick`, `handle_panel_leave[_ok]`, `handle_panel_group_debate`, `handle_panel_group_digest_mode` | 10/page, top-10 observed members inline; leave via `bot.leave_chat`, confirm-gated, audited; digest Standard/Off flip (any admin, audited; profiles in Stage B) |
| Features (`pnl_features/feat_*`) + Settings (`pnl_settings/set_*/setdef_*/limits/limadd/limdel_*`) | callbacks + typed input, any-admin | `handlers/panel.py` + `services/panel_settings.py` | `handle_panel_features/flip`, `handle_panel_settings/detail/reset`, `handle_panel_limits/add/del`, `feature_enabled()`, `setting_value()` | 11 global toggles (admins exempt, fail open); 7 clamped settings + per-user rate overrides (one cached query), old→new audited |
| Reset user data (`pnl_reset_*/reset_ok_*`) | callbacks, owner-only, private-only | `handlers/panel.py` + `db.reset_user_data()` | `handle_panel_reset[_ok]` | Count-first confirm; one transaction + rollback; group history/summaries/tasks kept; bans/audit/admins kept; in-memory state cleared; post-reset writes tombstoned |
| Jobs (`pnl_jobs/jp_*/jr_*/jrun*`) + Stats (`pnl_stats`) | Jobs view any-admin, pause/run owner-only; Stats any-admin view-only | `handlers/panel.py` + `services/panel_jobs.py` | `handle_panel_jobs/pause/resume/backup_run[_ok]`, `handle_panel_stats`, `job_snapshot()`, `job_pause/resume()` | Pause = `job.<name>.paused_until` (cached, fail open; sched/backup ≤24h, relay/profiler indefinite); backup run-now = snapshot+send only, single-flight; stats = METRICS + Iran-day buckets (60s cache) |
| `/digest` | `commands=[digest]`, owner-only + private only | `handlers/admin.py` | `handle_digest_command` | `broadcast_daily_digest`; empty build = failure report |
| Broadcast (`pnl_bcast_menu/bc_users/groups/both/one`, typed chat/content, `pnl_bc_ok_*` confirm, `pnl_bc_stop`) | owner-only private panel flow; single-use 10-min draft token; one run at a time | `handlers/panel.py` + `db.panel_broadcasts` | `handle_panel_broadcast_menu/target/one/confirm/stop`, `_broadcast_worker`, `_send_broadcast_one` | Recipients frozen at draft; banned skipped + counted; 403/unreachable never retried, flood-wait retried ≤3; ≤20/s; progress every 10; audit start/end; runs listed (last 10) |
| Legacy reply-keyboard broadcast | — | REMOVED in Stage 3A (replaced by above) | — | `broadcast_message`, `_admin_broadcast_sessions`, `get_admin_keyboard` gone; orphan refs grepped clean |
| `/stats` | `commands=[stats]`, admin-only | `handlers/admin.py` | `handle_stats` | `METRICS` + known chats/users + active debates |
| `/todo [@user text]` | `commands=[todo]` | `handlers/tasks.py` | `handle_todo_command` | Opens priority keyboard; session in `_todo_sessions` |
| `todo_pri_*` / `todo_cat_*` / `todo_dl_none_*` / `todo_dl_custom_*` / `todo_dl_retry_*` / `todo_rec_*` / `todo_customrec_*` | callback prefixes | `handlers/tasks.py` | `handle_todo_priority/category/deadline_none/deadline_custom/deadline_retry/recurrence/custom_recurrence` | Wizard chain priority→category→deadline→recurrence; custom steps use `_waiting_text_input` |
| `wait_cancel_*` | callback prefix | `handlers/tasks.py` | `handle_wait_cancel` | Clears todo + remind waiting state |
| `/remind [@user text]` | `commands=[remind]` | `handlers/tasks.py` | `handle_remind_command` | Time keyboard (`10m/30m/1h/3h/tom9/custom`); delegated skips mention step |
| `remind_time_*` / `remind_ment_*` / `remind_rec_*` / `remind_customtime_*` / `remind_customrec_*` | callback prefixes | `handlers/tasks.py` | `handle_remind_time/mention/recurrence/custom_time/custom_recurrence` | Writes `reminders` row via `_finalize_reminder_save` |
| Wizard free-text input | func `_is_waiting_text_input`, text | `handlers/tasks.py` | `handle_waiting_text_input` → 4× `_handle_*_text` | Parses via `timeutil`; retry/cancel keyboards on error |
| `/todos` | `commands=[todos]` | `handlers/tasks.py` | `handle_todos_command` | Active (Done/Del buttons) + expired section |
| `todo_done_*` / `todo_del_*` | callback prefixes | `handlers/tasks.py` | `handle_todo_done/delete` | Creator-or-admin + same-chat IDOR guard |
| `remind_snooze_*` / `remind_dismiss_*` | callback prefixes | `handlers/tasks.py` | `handle_remind_snooze/dismiss` | Snooze +15m; dismiss rolls daily/weekly/custom forward |
| `/reset` | `commands=[reset]` | `handlers/basics.py` | `handle_reset` | Private → `/session` hint; group admin wipes `messages+summaries` (facts kept) |
| `/stop` | `commands=[stop]` | `handlers/basics.py` (+ Light copy in `handlers/chat.py`) | `handle_stop` → `_handle_stop_generic` | Private: cancel own run; group: must reply to placeholder, owner-or-admin |
| `/pic prompt` | `commands=[pic]` | `handlers/basics.py` | `handle_image_generation` | `agnes.generate_image`; rate cat `pic` |
| `/start` (L) | `commands=[start]` | `handlers/basics.py` | `handle_start` | Static intro + session hints |
| `/session [id] [title] / delete` | `commands=[session]`, private only | `handlers/basics.py` | `handle_session_command` | list/switch/rename/delete; max 20/user |
| `/new` | `commands=[new]`, private only | `handlers/basics.py` | `handle_new_session` | Creates + activates session |
| `sessw_*` (`sessw_{owner}_{sid}`) | callback prefix | `handlers/basics.py` | `handle_session_switch` | Owner-only inline switch |
| Inline ask (both bots) | `register_inline_handler(func True)` | `handlers/surfaces.py` | `_handle_inline_query_generic` | Instant placeholder article, **no LLM**; needs BotFather `/setinline` |
| Inline answer (both bots) | `register_chosen_inline_handler` | `handlers/surfaces.py` | `_handle_chosen_inline_result_generic` | LLM + edit via `inline_message_id`; memory `inline_[light_]<uid>`; needs `/setinlinefeedback 100%` |
| Guest-mode mention (both bots) | `register_guest_message_handler` | `handlers/surfaces.py` | `_handle_guest_message_generic` | Sync `answerGuestQuery`; routes: IG link → cached media, `/tr` → text, other `/cmd` → refused, else chat; memory `guest_[light_]<uid`; needs lib ≥4.36 + BotFather Guest Mode |
| `/tr [lang] [text]` + photo-with-`/tr`-caption | `commands=[tr]` + photo func `/^\/tr/` | `handlers/surfaces.py` | `handle_translate` | Vision-capable, `/stop`-aware; empty → `PERSIAN_GUIDE` |
| `/stopall` (L + Light) | `commands=[stopall]`, admin-only | `handlers/debate.py`, `handlers/chat.py` | `handle_stopall`, `handle_stopall_light` | `stop_all_debates()` |
| Debate trigger phrase | text `L/ال…` containing `با لایت` + `بحث` | `handlers/chat.py` → `services/debate.py` | `is_debate_trigger` → `start_debate` | L opener + `_debate_loop` thread (both bots reply-chain); blocked while a debate is active |
| Instagram link | func `_is_instagram_link_message`, text | `handlers/medialink.py` | `handle_instagram_link` | yt-dlp + OG fallback; single→photo/video, multi→mediagroup (cap 10); marks `bot_media_messages` |
| YouTube link | func `_is_youtube_link_message`, text | `handlers/medialink.py` | `handle_youtube_link` | `_extract_youtube_qualities` → picker (`_youtube_quality_sessions` token) |
| `yt_dl_{token}_{key}` | callback prefix | `handlers/medialink.py` | `handle_youtube_quality_choice` | Claim-pop (no double-send); oversize → userbot send |
| `/tldr [N]` | `commands=[tldr]`, groups only | `handlers/memory_cmds.py` | `handle_tldr` | `chat_log` window (time or last-N), per-group cooldown |
| `/myfacts` | `commands=[myfacts]` | `handlers/memory_cmds.py` | `handle_myfacts` | Chat-scoped fact list (numbers match `/forget`) |
| `/forget <n\|all>` | `commands=[forget]` | `handlers/memory_cmds.py` | `handle_forget` | Single-fact or full wipe |
| Main chat (text/photo) | `content_types=[text, photo]` catch-all | `handlers/chat.py` | `handle_all_messages` | Full pipeline (§4); Light mirror `handle_light_messages` (text-only, memory `light_<chat>`) + `handle_start_light` |
| Reminder/todo firing | scheduler claim batches (not Telegram updates) | `services/scheduling.py` | `_fire_reminder`, `_fire_todo_expiry` | Alarm + snooze/dismiss buttons; recurrence roll-forward |
| Daily digest | scheduler / `/digest` (owner-only) / panel send-now | `services/digest.py` | `run_digest_cycle()`, `broadcast_daily_digest()`, `deliver_to_group()` | Per-group modes (standard/off/profile) + one build per profile + budget gate + delivery ledger + unreachable marking; five-way counts; per-profile repeat guard |
| Daily backup | scheduler 04:00 Iran | `services/backup.py` | `send_daily_backup` | Purge + VACUUM → gzip snapshot → admin DM + rotation |

## 12. Background jobs, schedulers, error handling

- **Reminder/todo dispatcher** (`services/scheduling.py: _reminder_scheduler_loop`): 15s poll, DB claim-token batches (`CLAIM_STALE_SECONDS=300`), thread-pool fire (`SCHEDULER_MAX_WORKERS=5`); restart catch-up fires missed items; transient errors release claim, permanent (400/401/403/404) count to `MAX_DISPATCH_RETRIES` then force-dismiss + admin alert.
- **Digest** (`_digest_scheduler_loop`): sleeps to next `DIGEST_HOUR_IRAN:MINUTE`, boot catch-up if started late, never dies (errors → admin notify).
- **Backup** (`_backup_scheduler_loop`): 04:00 Iran; `_run_daily_maintenance` (purge `DB_RETENTION_DAYS`, weekly VACUUM, size warn `DB_SIZE_WARN_MB`) then snapshot via SQLite backup API → gzip → admin DM + keep `BACKUP_KEEP_DAILY`.
- **Fact extractor** (`fact_extractor.run_forever`): queued worker, 45s/user cooldown, ≤3 facts/turn, cap 50/user.
- **Behavior profiler** (`_behavior_profiler_loop`): daily 03:30 Iran, ≤25 users (≥10 msgs), transcript ≤16k chars, profiles saved as group facts.
- **Session GC** (`_session_gc_loop`): every `SESSION_GC_INTERVAL` sweeps TTL stores + `bot_media_messages` (>60d) + `chat_log` (>48h).
- **Userbot thread** (`_userbot_thread_main`): no-op unless `TG_API_ID/HASH` + session file. **IG relay** (`run_forever`): polls every `IG_RELAY_POLL_SECONDS`, idle unless creds+enabled+target+threads.
- **Errors:** every handler wraps in try/except → user fallback message; `AdminNotifier.notify` throttled per-type (`ADMIN_ERROR_THROTTLE_SECONDS`); poller crash → restart + `poller_restarts` metric; scheduler/digest/backup loops catch per-run errors and continue.
- **Logging:** `config.py: basicConfig(INFO, "%(asctime)s | %(level)s | %(name)s | %(message)s")`, logger name `L-bot` (`ig-relay` for relay); stdout (platform logs) + Local-API `data/botapi/log.txt`.

## 13. Conventions and gotchas

- **Dispatch order is the feature:** telebot first-match-wins — new handlers must be registered in `wiring.register_all()` *ahead* of the catch-all (`handlers.chat` last); `commands=` only inspects `message.text`, so photo-caption commands need an extra `func` registration (see `/tr`).
- **Callback data prefixes are the routing contract** (see §11 list); callback format `{prefix}_{ownerid}` + owner check on every button. **Exception:** `pnl_` panel callbacks use numeric-only payloads and skip the `{prefix}_{ownerid}` convention — `panel_guard` re-checks private chat + admin on every update instead.
- **Admin identity:** owner = `ADMIN_IDS[0]` (else legacy `ADMIN_ID`); `is_admin()` = env set or `panel_admins` row via 10s full-set cache (zero warm reads; fail closed); bans via 10s full-set cache (fail open). Central `process_new_updates` gate enforces bans + maintenance for both bots before dispatch; maintenance acks (callback popup, inline empty+button, one-shot guest answer) go out on bounded daemon threads.
- **Claim-token dispatch:** scheduler finalizes are conditional on the seen trigger/deadline value — never "fix" a duplicate send by removing the guard; the dup-send window is irreducible (no idempotent Telegram send).
- **TTL sessions everywhere** for wizard state; key `(chat_id, user_id)`, admin-broadcast keyed by `user_id` alone. Never use `register_next_step_handler` (doesn't suppress dispatch — broadcast text used to leak into chat history).
- **Memory namespaces:** `chat_id` strings double as scopes (`<uid>#<sid>`, `light_*`, `inline_*`, `guest_*`, `debate_*`, `tldr_*`); keep the prefix scheme when adding bots/surfaces.
- **Markdown fragility:** user text inside `` ` `` spans goes through `sanitize_backtick_span`; elsewhere `escape_markdown` — raw user text in `parse_mode="Markdown"` returns 400.
- **`group_members` ≠ roster:** only users observed since join (platform limit); "tag everyone" is best-effort. Guest/inline memories are per-*person* (`guest_/inline_<uid>`) — the bot can't see the foreign chat.
- **BotFather prerequisites:** Light/inline need `/setinline` + `/setinlinefeedback 100%`; guest mentions need Guest Mode enabled per bot.
- **Known dead/duplicate code (do not "fix" blindly):** `bots.py: _BOT_TOKENS` is written but never read (leftover of removed raw Rich-Message calls); `services/scheduling.py` constructs `_scheduler_executor` twice (second wins, harmless). UNCLEAR: none blocking — every map statement was read from code; `data/botapi/` contents are Local-API runtime files, not source.
- **No TODO/FIXME markers** found in source (only `TODO` substrings inside log strings like "TODO COMMAND ERROR").

## 14. "Where do I change X?" quick lookup

1. Add a new command → handler in the right `handlers/*.py` (+ import in `wiring.register_all()` *before* `handlers.chat`), menu entry in `wiring._sync_bot_commands()`, row in §5.
2. Change a reply/message text → edit at its call site (owner file in §5; personas/prompts in `services/memory.py`, `/tr` guide in `handlers/surfaces.py`).
3. Change a keyboard/button → builder next to its flow (§11 `docs/map/config.md`); keep `callback_data` prefix stable.
4. Add a callback button → new `callback_query_handler` with unique prefix + owner/IDOR check (copy `handle_todo_done`), sender button with same prefix.
5. Add a wizard step → TTL store in `sessions.py` + interceptor func registered before catch-all (pattern: `handlers/tasks.py` waiting-text flow).
6. Add a DB field → `db.py` `ChatRepository._init` (new table or guarded `ALTER TABLE`), repo accessors, note in `docs/map/data.md`.
7. New table → same + include in `tests/test_smoke.py` expected tables + `tools/verify_restore.py` core set if critical.
8. Change memory/context → `services/memory.py` (`build_context`, `maybe_summarize`, thresholds in `config.py`).
9. Change persona/voice → `SYSTEM_PROMPT` / `SYSTEM_PROMPT_LIGHT` (`services/memory.py`), debate prompts (`services/debate.py`).
10. Add/schedule a job → daemon thread start in `main.main()` + loop fn (pattern: `services/digest.py`), errors must notify + continue.
11. Change digest content/schedule → `services/digest.py` (`DIGEST_QUERIES`, prompt) + `DIGEST_HOUR_IRAN/MINUTE` env.
12. Change backup/retention → `services/backup.py` + `DB_RETENTION_DAYS/BACKUP_KEEP_DAILY/DB_SIZE_WARN_MB` env; procedure in `RESTORE.md`.
13. Add an API integration → client call via `infra.HTTP`, key in `config.py` (new env), quirks row in `docs/map/integrations.md`; never log the key.
14. Add an env var → `config.py` (+ default), `.env` (no value committed), rows here + `docs/map/config.md`.
15. Touch IG downloads → `services/media.py` (`download_instagram_post`, `_pick_media_url`, OG fallback); cookies via `INSTAGRAM_COOKIES_FILE`.
16. Touch YT downloads → `services/media.py` (qualities/download, ffmpeg/deno fallbacks); handler in `handlers/medialink.py`.
17. Touch relay → `ig_relay.py` only (store/config/poll/command); deps injected via `wiring._ig_relay_deps`.
18. Admin-only feature → `handlers/shared.is_admin()` gate + admin keyboard/menu if needed; broadcast targeting reads `known_chats`.
19. Rate-limit tuning → `limits.py: _limits` (+ new category string where checked).
20. `/stop`-aware long op → `_register_active_request` before work, check entry `cancel` in loop, `_consume_active_request` after (pattern: `services/streaming.py` + `handlers/chat.py`).
21. bot_media (re-uploaded media) exemption → `repo.mark_bot_media_message` at send, `is_bot_media_message` check exists in both chat handlers.
22. Debate behavior → `services/debate.py` (trigger regex, prompts, `MAX_DEBATE_ROUNDS/DEBATE_DELAY`); `/stopall` in `handlers/debate.py` (+ Light copy in `handlers/chat.py`).
