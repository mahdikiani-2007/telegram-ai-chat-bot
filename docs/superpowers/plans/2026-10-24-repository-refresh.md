# Repository Documentation Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the published Telegram bot repository documentation and repository-facing metadata in line with the current modular Railway bot implementation without deleting unrelated user work.

**Architecture:** Treat the modular checkout at the repository root as the source of truth. Add accurate English and Persian README files that explain the current entrypoint, features, configuration, deployment, commands, and validation; keep implementation files unchanged unless inspection proves a documentation/configuration defect requires a surgical fix. Update the project map only for the documentation surface changed, then create a local commit and attempt a non-main remote update only if the inspected remote can accept it safely.

**Tech Stack:** Python 3.13, pyTelegramBotAPI 4.36.0, SQLite/WAL, Docker, Railway, pytest, shell tooling.

**Spec:** User request in the current session: refresh the existing repository for the significantly changed local bot, update all README documentation, remove or upgrade stale files/configuration, validate, and commit.

## Global Constraints

- Preserve unrelated user changes, including the existing untracked `docs/superpowers/plans/2026-10-08-digest-ui.md`.
- Do not commit secrets, runtime database files, `.env`, cookies, or session files.
- Do not change `Dockerfile`, `start.sh`, `requirements.txt`, `bot.py`, or `.gitignore` unless inspection identifies a direct documentation/configuration defect that cannot be fixed elsewhere.
- Railway deploys from `main`; work on the current feature branch and never push or merge `main`.
- Keep `PROJECT_MAP.md` under approximately 500 lines, with no secrets or line-number references.

## Review Focus

- A fresh clone must use the documented root entrypoint and not assume a removed `refactored/` directory; verify the README paths against `bot.py`, `main.py`, `start.sh`, and `Dockerfile`.
- Environment-variable examples must match `.env.example` and `config.py`, including dedicated vision settings and the optional Light bot; verify with a generated-name comparison.
- Deployment instructions must not promise obsolete Hugging Face/Render health-server behavior; verify the documented Railway/Docker flow against `Dockerfile` and `start.sh`.
- Feature and command lists must include the current panel, digest, relay, sessions, media, memory, and reminder surfaces without inventing unsupported commands; verify command names against `wiring.py` and handlers.
- Documentation refresh must not stage runtime artifacts or the user's existing plan; verify `git status`, staged file list, and secret-pattern checks before committing.

---

### Task 1: Capture the current repository contract

**Files:**
- Read: `PROJECT_MAP.md`
- Read: `docs/map/modules.md`
- Read: `docs/map/config.md`
- Read: `docs/map/integrations.md`
- Read: `docs/map/data.md`
- Read: `.env.example`
- Read: `Dockerfile`
- Read: `start.sh`
- Read: `wiring.py`
- Read: `handlers/*.py`
- Read: `services/*.py`

**Interfaces:**
- Produces: an evidence-backed list of supported commands, environment variables, deployment steps, tests, and repository paths used by Tasks 2–4.

- [ ] **Step 1: Compare the old published README and repository tree with the current tracked tree.**
- [ ] **Step 2: Record stale claims to remove or correct: Python/dependency versions, hosting platforms, health-check behavior, monolithic file layout, incomplete commands, and obsolete API variables.**
- [ ] **Step 3: Confirm no source/configuration change is required beyond documentation; if one is required, add the exact file and validation to the relevant task before editing.**

### Task 2: Add current English repository documentation

**Files:**
- Create: `README.md`
- Test: repository documentation checks in Task 4

**Interfaces:**
- Consumes: the source-of-truth contract from Task 1.
- Produces: a clone-ready English README with no unsupported claims.

- [ ] **Step 1: Write sections for overview, supported capabilities, architecture/layout, requirements, local setup, environment configuration, command reference, Railway/Docker deployment, persistence/backup, development checks, security, and contribution workflow.**
- [ ] **Step 2: Document `python bot.py` as the compatibility entrypoint while explaining that `main.py` owns startup and `start.sh`/Docker provide the Railway deployment path.**
- [ ] **Step 3: Document only environment variable names and safe placeholders; link readers to `.env.example`, `PROJECT_MAP.md`, `RESTORE.md`, and the relevant map details.**
- [ ] **Step 4: Include the current admin panel, daily digest/profile modes, Instagram relay, vision routing, sessions, tasks/reminders, media downloaders, inline/Guest Mode prerequisites, and the test/check commands required by repository policy.**

### Task 3: Add current Persian repository documentation

**Files:**
- Create: `README-fa.md`
- Test: repository documentation checks in Task 4

**Interfaces:**
- Consumes: the same source-of-truth contract and command/configuration decisions as `README.md`.
- Produces: a concise Persian README that mirrors the English setup, deployment, safety, and feature guidance without diverging behaviorally.

- [ ] **Step 1: Translate the operational sections and feature summary into Persian while preserving exact command names, file paths, environment-variable names, and code blocks.**
- [ ] **Step 2: Cross-check both README files for matching setup commands, deployment assumptions, security warnings, and links.**

### Task 4: Synchronize repository map and verify the refresh

**Files:**
- Modify: `PROJECT_MAP.md`
- Modify: `.env.example`
- Modify: `docs/map/modules.md` only if the README-facing entrypoint/layout facts require correction
- Modify: `docs/map/config.md` only if README-facing configuration facts require correction
- Test: `tests/` and repository checks

**Interfaces:**
- Consumes: README files from Tasks 2–3 and actual source/configuration.
- Produces: documentation and map entries that agree with the implementation plus a verified working tree.

- [ ] **Step 1: Update the map's `Last verified` line and only affected directory/documentation references; do not rewrite unrelated code inventory.**
- [ ] **Step 2: Run `python -m pytest -q tests/`.**
- [ ] **Step 3: Run `python tools/check_order.py`.**
- [ ] **Step 4: Run `python -m pytest -q tests/test_dispatch.py`.**
- [ ] **Step 5: Run focused repository checks for README links/paths, environment-name consistency, secret-pattern absence, and staged-file scope.**
- [ ] **Step 6: Inspect the final diff and commit with `docs: refresh repository documentation for modular bot`, including the required Copilot co-author trailer.**
- [ ] **Step 7: Attempt a safe non-main push/update only after confirming the intended remote and branch; if unavailable or unsafe, retain the complete local commit and report the exact blocker.**
