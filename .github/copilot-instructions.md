# Rules for AI coding agents in this repository

## Project and deployment
- Telegram bot, Python, pyTelegramBotAPI 4.36, two bot instances (L and Light), SQLite. Modular layout at the repository ROOT (no refactored/ folder).
- Railway deploys automatically from `main` via Dockerfile + start.sh. EVERY merge to main is a production deploy. Work on a branch, open a pull request, never push to main, never merge, never force-push, never delete branches.
- Do not change Dockerfile, start.sh, requirements.txt, bot.py or .gitignore unless the task explicitly says so. Do not add dependencies.
- Never print, log or commit secrets (bot tokens, API keys, cookies, .env, *.db, *.session). Tests are hermetic (tests/conftest.py stubs tokens and DATA_DIR); never use real credentials.

## Navigation and docs
- Read PROJECT_MAP.md first, then the linked docs/map/ files, and use it to find code instead of scanning the repo. If the map and the code disagree, trust the code.
- After changing code, update PROJECT_MAP.md and docs/map/* by hand, following .opencode/skills/update-project-map/SKILL.md (the OpenCode skill itself cannot run here). No line numbers, no secrets, keep PROJECT_MAP.md under ~500 lines.

## Verification (state exactly what you ran; never claim a test passed unless you ran it)
- python -m pytest -q tests/ ; python tools/check_order.py ; tests/test_dispatch.py must pass. Update tests/baseline_inventory.json ONLY for intended handler additions/removals and show its diff. New DB tables must be added to the expected tables in tests/test_smoke.py.
- A test function must assert, never return a value.
- Final report: files changed, test counts, anything skipped or unsure.

## Admin panel rules (handlers/panel.py, services/panel_settings.py, services/panel_jobs.py)
- Owner = first ADMIN_IDS entry (else ADMIN_ID). Owner-only: broadcast, reset user data, leave chat, add/remove admin, logs, DB file send, run-now/pause of jobs, digest profiles, global digest run-all. Other admins: users, groups, bans, toggles, settings, statistics, per-group digest mode.
- Every panel entry point (command, callback, typed input) is wrapped by panel_guard: private chat only, fresh permission check on every update, silent deny for strangers/groups (never reveal the panel exists).
- Callback data: prefix pnl_ and numeric-only payloads (64-byte limit). Escape and truncate all Telegram-user-originated text (titles, names, usernames). LIKE searches use ESCAPE and escape % _ and the escape char.
- Every mutation is written to the audit log (old -> new). Destructive or wide-impact actions need a confirm step. Admins/owner can never be banned or reset; nobody can ban/reset themselves.
- Central ban + maintenance gate wraps process_new_updates on BOTH bots and all update types; it must fail open (log, dispatch normally), never block the polling thread, and do zero DB reads per update when caches are warm. Admin status beats a ban row.
- Caches: 10 s TTL, immediate invalidation on panel writes, copy-and-swap (never mutate shared structures in place). Fail open for toggles/bans/maintenance/modes; fail closed for DB-granted admin permission (env-listed admins always pass).
- Settings: hard min/max clamps; the panel must reject values where 0 or negative means "unlimited/disabled" unless documented; invalid stored values fall back to defaults. Never make secrets, models, endpoints or paths editable.
- Scheduled/background work must never run on the polling thread, and background writers must respect reset tombstones.
- Ban only affects incoming updates and a banned user's own private-chat reminders; group reminders and digests keep working.

## Digest rules (services/digest.py)
- Tavily budget is shared with chat web search; the digest must keep a reserve and never exhaust it. One build per distinct profile (never per group); a custom profile with no search results must not call the LLM and sends nothing. Topic text is untrusted data inside delimiters. Preview never writes to the ledger or digest_history. Digest is sent only through the main bot. Counts are reported separately: sent / failed / unreachable / skipped-off / skipped-budget / skipped-no-results.
