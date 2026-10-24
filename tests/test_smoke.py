"""Smoke tests — behavior-preservation contract for the refactor.

Run:  python3 -m pytest tests/ -q
Works against the monolith AND the modular layout: attributes are
resolved from their new home first, falling back to `bot`.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Env is set hermetically by tests/conftest.py before any import happens.
# (Kept as documentation; do NOT re-set DATA_DIR here — db.repo binds it
# at first import, and whichever test module imports first would win.)

EXPECTED_BOT_TABLES = {
    "messages", "summaries", "todos", "reminders", "known_chats",
    "group_members", "bot_media_messages", "sessions",
    "user_active_sessions", "digest_history", "user_facts",
    "behavior_stats", "chat_log",
    "panel_settings", "panel_admins", "panel_bans", "panel_audit",
}
EXPECTED_RELAY_TABLES = {"ig_relay_config", "ig_relay_seen"}


def _resolve(mod_names, attr):
    for mod_name in mod_names:
        try:
            mod = __import__(mod_name, fromlist=[attr])
        except ImportError:
            continue
        if hasattr(mod, attr):
            return getattr(mod, attr)
    raise AssertionError(f"{attr} not found in {mod_names}")


def _bot_module():
    # After cutover, `main`/`wiring` own startup; before that, `bot`.
    for name in ("wiring", "bot"):
        try:
            return __import__(name)
        except ImportError:
            continue
    raise AssertionError("neither wiring nor bot importable")


def test_import_and_db_schema():
    entry = _bot_module()
    db_file = _resolve(("config", "bot"), "DB_FILE")
    assert os.path.isfile(db_file), f"DB not created at {db_file}"
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    missing = EXPECTED_BOT_TABLES - tables
    assert not missing, f"missing tables: {missing}"
    # ig_relay tables are created lazily via RelayStore on first deps use
    import ig_relay
    deps_fn = _resolve(("wiring", "bot"), "_ig_relay_deps")
    deps = deps_fn()
    store = ig_relay.RelayStore(deps["db_file"])
    assert store is not None
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert EXPECTED_BOT_TABLES | EXPECTED_RELAY_TABLES <= tables, \
        f"missing: {(EXPECTED_BOT_TABLES | EXPECTED_RELAY_TABLES) - tables}"


HANDLER_MODULES_CANONICAL = [
    "handlers.guard", "handlers.admin", "handlers.tasks",
    "handlers.basics", "handlers.surfaces", "handlers.debate",
    "handlers.medialink", "handlers.memory_cmds", "handlers.chat",
]


def _ensure_handlers_registered():
    """Register handlers via the true entry path when available."""
    try:
        import wiring
        wiring.register_all()
        return list(HANDLER_MODULES_CANONICAL)
    except ImportError:
        pass
    done = []
    for name in HANDLER_MODULES_CANONICAL:
        try:
            __import__(name)
            done.append(name)
        except ImportError:
            pass
    return done


def test_bot_objects_and_handler_counts():
    try:
        import bots as bots_mod
    except ImportError:
        import bot as bots_mod
    assert getattr(bots_mod, "bot", None) is not None
    done = _ensure_handlers_registered()
    if len(done) < len(HANDLER_MODULES_CANONICAL):
        pytest.skip(f"handlers migration in progress ({len(done)}/{len(HANDLER_MODULES_CANONICAL)} modules)")
    n_msg = len(getattr(bots_mod.bot, "message_handlers", []))
    n_cb = len(getattr(bots_mod.bot, "callback_query_handlers", []))
    # Digest-topics Stage A baseline (no LIGHT token): 27 message (+/reminders);
    # callbacks 82 (+reminder delete) = 66 + group digest-mode switch + digest overview/groups/profile callbacks.
    assert n_msg == 27, f"message handlers: {n_msg}"
    assert n_cb == 82, f"callback handlers: {n_cb}"
    assert getattr(bots_mod, "bot_light", None) is None  # no LIGHT token in smoke env


def test_handler_functions_importable():
    entry = _bot_module()
    mods = ("handlers.guard", "handlers.admin", "handlers.panel", "handlers.tasks",
            "handlers.basics", "handlers.surfaces", "handlers.debate",
            "handlers.medialink", "handlers.memory_cmds", "handlers.chat",
            "wiring", "bot")
    for attr in ("handle_all_messages", "handle_todo_command",
                 "handle_remind_command", "handle_admin_panel",
                 "handle_waiting_text_input", "handle_tldr",
                 "handle_myfacts", "handle_forget"):
        assert hasattr(entry, attr) or _resolve(mods, attr), attr


def test_media_and_relay_seam():
    download = _resolve(("services.media", "bot"), "download_instagram_post")
    extract = _resolve(("services.media", "bot"), "extract_instagram_urls")
    assert callable(download) and callable(extract)
    deps_fn = _resolve(("wiring", "bot"), "_ig_relay_deps")
    deps = deps_fn()
    assert set(("metrics", "admin_notifier", "mark_bot_media",
                "download_post", "text_url_extractor",
                "upload_max_bytes", "db_file")) <= set(deps)
    assert deps["download_post"] is download
    assert deps["text_url_extractor"] is extract


def test_scheduler_and_worker_targets():
    for mod_name, attr in (
            ("services.scheduling", "_reminder_scheduler_loop"),
            ("services.backup", "_backup_scheduler_loop"),
            ("services.digest", "_digest_scheduler_loop"),
            ("services.memory", "fact_extractor"),
            ("services.debate", "stop_all_debates"),
            ("sessions", "_session_gc_loop")):
        assert _resolve((mod_name, "bot"), attr) is not None, \
            f"{mod_name}.{attr}"


def test_db_roundtrip():
    repo = _resolve(("db", "bot"), "repo")
    repo.save_message("smoke_test_chat", "user", "hello smoke")
    hist = repo.get_history("smoke_test_chat", limit=5)
    assert hist and hist[-1]["role"] == "user" \
        and "hello smoke" in hist[-1]["content"]
    repo.clear_chat("smoke_test_chat")
    assert repo.count_messages("smoke_test_chat") == 0
