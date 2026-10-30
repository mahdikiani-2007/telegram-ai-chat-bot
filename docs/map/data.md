# Data layer + state management

## Database

- **Type:** SQLite file at `DATA_DIR/chat_history.db` (default `data/` next to code; Railway volume at `/data`).
- **Access:** `db.py: ChatRepository` singleton `repo`. Thread-local persistent connection per worker thread (max ~8), single `_write_lock` serializes mutations. `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, 30s busy timeout. Schema self-migrates on boot (`CREATE TABLE IF NOT EXISTS` + guarded `ALTER TABLE`; missing cols backfilled).
- **Shutdown:** `repo.checkpoint_wal()` runs in `_shutdown_sequence`.
- **Restore:** see `RESTORE.md` + `tools/verify_restore.py`. Purge policy: `messages` older than `DB_RETENTION_DAYS` (default 90) deleted daily pre-backup; summaries/facts/sessions/todos/reminders never purged.

## Tables (writer → readers)

| Table | Key fields | Written by | Read by |
|---|---|---|---|
| `messages` | `id, chat_id TEXT, role, text, timestamp` (`chat_id` = group id, user id, `<uid>#<sid>` session ns, `light_*`, `inline_*`, `guest_*`, `debate_*`, `tldr_*` cooldown markers) | `repo.save_message` ← handlers.chat/surfaces (every turn) | `get_history` (last-N verbatim), `maybe_summarize`, fact extractor |
| `summaries` | `chat_id PK, summary, updated_at` (rolling "case file" per memory ns) | `repo.save_summary` ← `maybe_summarize` | `build_context[_light]` (prepended to system prompt) |
| `todos` | `id, chat_id, creator_id, text, priority, category, status (0 active/1 done/2 expired), deadline REAL, recurrence, assignee_id/username, attempts, claimed_at, claim_token` | handlers.tasks wizard (`add_todo`); scheduler (`mark_todo_done/expired`, claim cols) | `/todos`, done/del callbacks, scheduler claims |
| `reminders` | `id, chat_id, creator_id, text, mention_type (me/all/assignee), recurrence (none/daily/weekly/custom), trigger_time REAL, status, deadline, assignee_id/username, attempts, claimed_at, claim_token` | handlers.tasks wizard (`add_reminder`); scheduler | `/reminders`, scheduler claims, snooze/dismiss/del callbacks |
| `known_chats` | `chat_id PK, type, username, title, last_seen` | middleware (`upsert_known_chat`, every message) | broadcast targets, `/stats` counts, group list |
| `group_members` | `(chat_id, user_id) PK, username, first_name, last_seen` — only users the bot *observed*, never full roster (platform limit) | middleware (`upsert_group_member`, groups only) | `@` delegation resolve, "tag everyone" mention builder, reminders `all` |
| `bot_media_messages` | `(chat_id, message_id) PK, kind, created_at` (GC'd >60d by session-GC loop) | IG/YT senders + ig_relay (`mark_bot_media_message`) | chat handlers skip AI reply when replying to re-uploaded media |
| `sessions` | `(session_id, user_id) PK, title, created_at` (private chats; max 20/user) | `/new`, lazy auto-create on first DM | `/session` list/switch/rename/delete |
| `user_active_sessions` | `user_id PK, active_session_id NULL` (NULL = legacy plain-`uid` memory) | `create_session/set_active_session`, cleared on delete | `_resolve_private_session` per DM |
| `digest_history` | `id, digest_date (Iran YYYY-MM-DD), profile_key ('standard' default), line` (7-day rolling) | `save_digest_lines` after each successful build (per key) | repeat-guard injected into writeup prompt (per key) |
| `digest_group_config` | `chat_id PK, mode standard/off/profile, profile_id NULL, updated_by, updated_at` (no row = standard) | panel group switch | mode lookup per digest run (cached) |
| `digest_profiles` | `id, name` (unique case-insensitively, 1–40 chars), `topic` (sanitized, ≤200), `created_by, created_at, previewed_at NULL` | profile backend | profile builds; assignment requires previewed |
| `digest_deliveries` | `(chat_id, digest_date) PK, profile_key, status, error, sent_at` | every digest send path | only `sent` is final; failed/unreachable/skip rows remain retryable the same day |
| `digest_unreachable` | `chat_id PK, consec_failures, last_failure_at, last_failure_date` (counter increments once per digest date; ≥3 held; cleared on success or newer last_seen) | send-failure classifier | skip logic |
| `user_facts` | `(user_id, fact) PK, source_chat_id, visibility (private/group), created_at` (cap 50/user) | fact extractor + behavior profiler (`add_user_facts`) | `_inject_user_profile` (scoped), `/myfacts`, `/forget` |
| `behavior_stats` | `(chat_id, user_id, stat_date) PK, msg_count, total_chars, reply/link/question/media counts, sample_lines (≤6×80ch), last_hour` (kept ~3d; `stat_date` is the IRAN local date, not UTC) | middleware (`record_behavior`, groups, zero-LLM) | nightly behavior profiler (default query = yesterday-Iran) |
| `chat_log` | `chat_id, user_id, first_name, text (≤1000ch), ts` (purged >48h by session-GC loop; buffer for profiler + `/tldr`) | middleware (`log_group_chat`; bots/commands excluded) | `/tldr`, behavior profiler transcript |
| `maintenance_meta` | `key PK, value` (e.g. `last_vacuum`) | `set_maintenance_value` | `maybe_vacuum` gating |
| `panel_settings` | `key PK, value` (toggles, numeric overrides, maintenance + notice keys; digest reserve/max profiles/preview allowance) | panel (`set_setting`), Stage 2+ writers | `get_setting` (10s cached) |
| `panel_admins` | `user_id PK, added_by, added_at` (DB-managed admins; owner/env IDs never stored here) | owner via panel | `is_panel_admin` ← `is_admin()` |
| `panel_bans` | `user_id PK, reason, banned_by, created_at` (app-level bans; admins exempt) | panel Stage 2 | central gate `is_banned()` |
| `panel_audit` | `id, admin_id, action, target, result, created_at` (pruned to newest 2000 on write) | `write_panel_audit` ← panel mutating actions | panel Audit viewer |
| `panel_broadcasts` | `id, admin_id, target, content_type, total/sent/unreachable/failed/skipped_banned, status running\|done\|stopped\|interrupted, started/finished_at` (counters only — never content) | broadcast runner (progress + finish); startup marks leftovers `interrupted` | panel recent-runs list |
| `miniapp_scores` | `(scope_key, user_id, game) PK, best, plays, last_name, updated_at` — `scope_key` = `global` or `g<chat_id>` (from signed `start_param`); best-only updates | Mini App API (`upsert_miniapp_score` on every finished run) | `/api/leaderboard` + `miniapp_user_rank` (games lobby/boards) |
| `game_rooms` | `id PK, game_type, status, player1_id, player2_id, white_player_id, black_player_id, state_json, current_seq, initial_time_ms, increment_ms, white_remaining_ms, black_remaining_ms, turn_started_at, invite_code, created_at, updated_at, expires_at, result` | `repositories.games.GameRepository` (`create_room`, `join_room`, `update_room_state`, `set_room_result`) | `ChessService` room creation, joining, moves, and startup state recovery |
| `game_moves` | `id PK AUTOINCREMENT, room_id, seq, player_id, move_id, action_json, uci, san, fen_after, time_remaining_ms, created_at` (UNIQUE `room_id, seq`, UNIQUE `room_id, move_id`) | `repositories.games.GameRepository: save_move` on accepted moves | `ChessService` state synchronization, move history replay, and crash recovery |
| `game_stats` | `(user_id, game_type) PK, wins, losses, draws, games_played, kot_wins, hakem_kot_wins, win_streak, longest_win_streak` | `repositories.games.GameRepository: update_stats` on game finish | `ChessService`, `HokmService`, `/api/chess/profile`, `/api/hokm/profile` |
| `game_players` | `(room_id, seat) PK, user_id, team, player_type, ai_difficulty, connection_status, joined_at` | `repositories.games.GameRepository: save_hokm_room` | `HokmService` room seats and recovery |
| `game_events` | `id PK AUTOINCREMENT, room_id, seq (UNIQUE), actor_id, event_type, event_payload, request_id, created_at` | `repositories.games.GameRepository: record_game_event` | `HokmService` event ordering and idempotency |
| `game_ratings` | `(user_id, game_type) PK, rating, tier, matches_played, wins, losses, updated_at` | `repositories.games.GameRepository: update_rating_match` | `HokmService` Elo calculation, `/api/hokm/profile`, leaderboard |
| `match_history` | `id PK AUTOINCREMENT, room_id, game_type, winner_id, winner_team, final_score, hands_played, kot_count, hakem_kot_count, match_duration_seconds, completed_at` | `repositories.games.GameRepository: record_match_history` | Match results, post-game summary, stats |
| `ig_relay_config` | `key PK, value` (`enabled, target_chat, threads` JSON) | `/relay on/off/target/thread` | `InstagramRelay.ready/get_target/get_threads` |
| `ig_relay_seen` | `message_id PK, thread_id, attempts, done_at` (pruned to newest 2000) | relay poll loop (`seed_thread/record_attempt/mark_done`) | dedupe + retry counting |

## State management

- **Wizard/session state:** `sessions.py: TTLSessionStore` (thread-safe dict + per-entry TTL, default 900s; lazy expiry + `_session_gc_loop` sweep every 300s). Stores: `_waiting_text_input` (+ `WAITING_FOR_REMINDER_TIME/REMINDER_RECURRENCE/TODO_DEADLINE/TODO_RECURRENCE` consts), `_todo_sessions/_todo_pending_cat/_todo_pending_dl/_todo_pending_rec`, `_remind_sessions/_remind_pending_mention/_remind_pending_recurrence`, `_panel_input_sessions` (`{state}` incl. `PANEL_BROADCAST_CHAT/CONTENT`; panel draft tokens live in a separate 10-min in-memory store in `handlers/panel.py`, never in the DB). Keyed `(chat_id_str, user_id)` — except `_panel_input_sessions`, keyed by `user_id`. YT quality picker sessions live in `services/media.py: _youtube_quality_sessions` (token → `{url, title, options, user_id}`), swept by the same GC loop via `_register_late_session_store`.
- **`/stop` registry:** `services/streaming.py: _active_requests` (`(chat_id, user_id)` → entry `{cancel, placeholder_id}`) + `_all_inflight_ids`; group stops resolve the run via replied-to placeholder id.
- **Debates:** `services/debate.py: _active_debates` (`chat_id` → `{active, topic, round}`) under `_debate_lock`; stopped via `stop_all_debates()`.
- **Bot identity cache:** `bots.py: _bot_identity/_bot_light_identity` (`{username, id}`), lazy `get_me()` + locks; warmed once in `main()`.
- **IG relay runtime:** `ig_relay.py: InstagramRelay.state` (`logged_in/fatal/challenge/last_error/last_poll_at`) + `_challenge_until` pause timestamp; `RelayStore` persists config/ledger in DB.
- **YT extraction memory:** `services/media.py: _yt_last_good_mode` (`{use_cookies, player_client}`) biases next attempt order.
- **Panel runtime:** `services/panel_settings.py` 10s settings cache (invalidated on write; test hook `clear_settings_cache()`); maintenance-notice rate-limit map `(id(bot), uid) → ts`.
- **Search budget:** `services/search.py: tavily_budget` (`DailyBudget`, UTC-midnight reset).
- **Global counters:** `metrics.py: METRICS` dict (see modules ref).
