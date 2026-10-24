# Comprehensive README Documentation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the abbreviated English and Persian READMEs with complete, source-verified documentation for the current modular Telegram bot.

**Architecture:** Keep documentation at repository root and derive every operational claim from the current code, `.env.example`, deployment files, `PROJECT_MAP.md`, and `RESTORE.md`. Update only `README.md`, `README-fa.md`, and this plan; do not modify application code, tests, or release metadata.

**Tech Stack:** Python 3.13, pyTelegramBotAPI 4.36.0, SQLite/WAL, Docker, Railway, pytest, GitHub Markdown.

**Spec:** User request in the current session: comprehensively rewrite destination repository READMEs with verified architecture, prerequisites, all environment variables, local/polling/Docker/Railway operation, persistence, workflows, admin/security, validation, backup/restore, troubleshooting, deployment notes, and license.

## Global Constraints

- Branch from the destination repository's current `main`.
- Change only `README.md`, `README-fa.md`, and this documentation plan; do not change code.
- Document only variables and behavior present in `config.py`, `ig_relay.py`, `services/debate.py`, and `services/memory.py`.
- Reference `.env.example`, `PROJECT_MAP.md`, `docs/map/*`, and `RESTORE.md`; do not duplicate secrets.
- Preserve the existing `LICENSE` file and describe its MIT license accurately.
- Run documentation/link/config checks, commit, push, and open a normal pull request.

## Review Focus

- Missing or invented environment variables; verify every documented variable against source and `.env.example`.
- Incorrect startup/deployment claims; verify `bot.py`, `main.py`, `start.sh`, `Dockerfile`, and Railway volume behavior.
- Incomplete command/workflow coverage; verify command menus and handler registration in `wiring.py` and `handlers/`.
- Unsafe persistence or backup guidance; verify paths and restore commands against `db.py`, `RESTORE.md`, and `tools/verify_restore.py`.
- English/Persian documentation drift; verify both files contain matching setup, deployment, security, validation, and troubleshooting guidance.

---

### Task 1: Rewrite the English README

**Files:**
- Modify: `README.md`
- Read: `config.py`, `.env.example`, `main.py`, `wiring.py`, `Dockerfile`, `start.sh`, `RESTORE.md`, `LICENSE`, `PROJECT_MAP.md`, `docs/map/*.md`

**Interfaces:**
- Produces: a complete English README describing the real bot without changing application behavior.

- [ ] **Step 1: Write the overview and capability sections.**
- [ ] **Step 2: Document architecture, repository layout, startup flow, long polling, and handler order.**
- [ ] **Step 3: Document prerequisites, local installation, `.env.example`, and every supported environment variable with defaults/fallbacks and safety notes.**
- [ ] **Step 4: Document chat/media, tasks/reminders, memory, inline/Guest Mode, admin panel, digest, relay, debates, and background jobs with exact commands and prerequisites.**
- [ ] **Step 5: Document SQLite/WAL persistence, `DATA_DIR`, Docker/Railway deployment, volume requirements, backup/restore, troubleshooting, security, testing/validation, contribution/deployment notes, and MIT license.**

### Task 2: Synchronize the Persian README

**Files:**
- Modify: `README-fa.md`

**Interfaces:**
- Consumes: the verified behavior and terminology from Task 1.
- Produces: a complete Persian README with matching commands, variables, paths, deployment guidance, troubleshooting, validation, and license information.

- [ ] **Step 1: Mirror the English README's section coverage while preserving exact code blocks, commands, paths, and environment-variable names.**
- [ ] **Step 2: Cross-check English/Persian headings and operational facts for drift.**

### Task 3: Validate, commit, push, and open PR

**Files:**
- Test: `README.md`, `README-fa.md`, `.env.example`, repository links, Git diff

**Interfaces:**
- Consumes: both rewritten READMEs.
- Produces: a clean documentation-only commit on a branch based on current `main` and a normal PR.

- [ ] **Step 1: Check every local Markdown link and every documented uppercase environment variable against the source/configuration.**
- [ ] **Step 2: Check for secret-looking values, stale hosting claims, raw escaped newlines, and code-file changes.**
- [ ] **Step 3: Run `git diff --check` and a lightweight Python syntax sanity check without changing code.**
- [ ] **Step 4: Commit with `docs: comprehensively document modular bot`.**
- [ ] **Step 5: Push the branch without force and create a PR against `main`.**
