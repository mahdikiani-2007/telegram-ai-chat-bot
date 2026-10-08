"""Stage A digest topics: modes, profiles, ledger, budget gate, unreachable.

Hermetic like the other suites (conftest stub env + temp DATA_DIR).
Network/LLM/Telegram are stubbed throughout; unique chat ids per test
with cleanup so the shared temp DB stays clean.
"""
import os
import sys
import time
import types as std_types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import handlers.shared as shared
from db import repo
from services import digest as digest_mod
from services import panel_settings as ps
from telebot.apihelper import ApiTelegramException


def _ns(**kwargs):
    obj = std_types.SimpleNamespace()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _as_owner(monkeypatch, owner=991):
    monkeypatch.setattr(shared, "OWNER_ID", owner, raising=False)
    monkeypatch.setattr(shared, "ADMIN_IDS", (owner,), raising=False)
    monkeypatch.setattr(config_mod, "OWNER_ID", owner, raising=False)


class _FakeBot:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail or {}

    def send_message(self, chat_id, text, **kwargs):
        mode = self.fail.get(chat_id)
        if mode == "forbidden":
            raise ApiTelegramException("sendMessage", None, {
                "ok": False, "error_code": 403,
                "description": "Forbidden: bot was kicked from the group chat"})
        if mode == "boom":
            raise RuntimeError("boom")
        self.sent.append((chat_id, text))
        return _ns(message_id=1)

    def answer_callback_query(self, *a, **k):
        pass

    def edit_message_text(self, *a, **k):
        pass


class _FakeAgnes:
    def __init__(self):
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return "- خبر یک\n- خبر دو"


@pytest.fixture
def stubbed(monkeypatch):
    bot = _FakeBot()
    agnes = _FakeAgnes()
    monkeypatch.setattr(digest_mod, "bot", bot)
    monkeypatch.setattr(digest_mod, "agnes", agnes)
    monkeypatch.setattr(digest_mod, "tavily_web_search", lambda q: f"result for {q}")
    monkeypatch.setattr(digest_mod, "TAVILY_API_KEY", "test-key")
    return bot, agnes


def _seed_groups(*cids):
    for cid in cids:
        repo.upsert_known_chat(str(cid), "group", None, f"G{cid}")


def _clear_groups(*cids):
    with repo._write_lock:
        conn = repo._connect()
        for cid in cids:
            conn.execute("DELETE FROM known_chats WHERE chat_id = ?", (str(cid),))
            conn.execute("DELETE FROM digest_group_config WHERE chat_id = ?", (str(cid),))
            conn.execute("DELETE FROM digest_deliveries WHERE chat_id = ?", (str(cid),))
            conn.execute("DELETE FROM digest_unreachable WHERE chat_id = ?", (str(cid),))
        conn.commit()


def test_query_template_persian_and_latin():
    fa = digest_mod._profile_queries("هوش مصنوعی")
    assert fa[0] == "هوش مصنوعی" and "اخبار امروز" in fa[1]
    assert "latest news today" not in " ".join(fa)
    en = digest_mod._profile_queries("quantum computing")
    assert en == ["quantum computing", "quantum computing latest news today"]


def test_no_results_profile_skips_llm_and_send(stubbed):
    bot, agnes = stubbed
    orig_search = digest_mod.tavily_web_search
    digest_mod.tavily_web_search = lambda q: ""
    pid = repo.create_digest_profile("P", "some topic here", 991)
    repo.mark_digest_profile_previewed(pid)
    _seed_groups(910101)
    repo.set_digest_mode(910101, "profile", pid, 991)
    ps.invalidate_digest_modes()
    try:
        text = digest_mod.build_profile_digest_text("some topic here", f"profile:{pid}")
        assert text == ""
        assert agnes.calls == 0, "LLM must never run on empty results"
        report = digest_mod.run_digest_cycle(triggered_by="test")
        assert report["skipped_no_results"] >= 1
        assert all(c[0] != 910101 for c in bot.sent)
        assert 910101 not in [c for c, _t in bot.sent]
    finally:
        digest_mod.tavily_web_search = orig_search
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_profiles WHERE id = ?", (pid,))
            conn.execute("DELETE FROM digest_deliveries WHERE chat_id = '910101'")
            conn.commit()
        _clear_groups(910101)
        ps.invalidate_digest_modes()


def test_ledger_no_double_send(stubbed):
    bot, agnes = stubbed
    _seed_groups(910111, 910112)
    try:
        r1 = digest_mod.run_digest_cycle(triggered_by="test")
        mine1 = {c for c, _t in bot.sent} & {910111, 910112}
        assert mine1 == {910111, 910112}
        r2 = digest_mod.run_digest_cycle(triggered_by="test")
        mine2 = {c for c, _t in bot.sent} & {910111, 910112}
        assert mine2 == mine1, "second run must send nothing new to them"
        rows = repo.deliveries_for_date(
            __import__("timeutil").format_trigger_time_iran(__import__("time").time()).split(" ")[0])
        assert rows["910111"][1] == "sent" and rows["910112"][1] == "sent"
    finally:
        _clear_groups(910111, 910112)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history")
            conn.commit()


def test_budget_skip_exactness_and_no_fallback(monkeypatch, stubbed):
    from services.search import tavily_budget

    bot, agnes = stubbed
    old_limit = tavily_budget._limit
    tavily_budget.set_limit(2)  # standard build consumes exactly 2
    pid = repo.create_digest_profile("PB", "budget topic here", 991)
    repo.mark_digest_profile_previewed(pid)
    _seed_groups(910121, 910122)
    repo.set_digest_mode(910121, "standard", None, 991)
    ps.invalidate_digest_modes()
    repo.set_digest_mode(910122, "profile", pid, 991)
    ps.invalidate_digest_modes()
    try:
        report = digest_mod.run_digest_cycle(triggered_by="test")
        assert report["skipped_budget"] == 1, report
        assert all(c[0] != 910122 for c in bot.sent), "no standard fallback for skipped profile"
        assert any(c[0] == 910121 for c in bot.sent)
        row = repo.get_digest_delivery(910122,
            __import__("timeutil").format_trigger_time_iran(__import__("time").time()).split(" ")[0])
        assert row and row[1] == "skipped_budget"
    finally:
        tavily_budget.set_limit(old_limit)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_profiles WHERE id = ?", (pid,))
            conn.execute("DELETE FROM digest_history")
            conn.commit()
        _clear_groups(910121, 910122)
        ps.invalidate_digest_modes()


def test_unreachable_mark_and_reactivation(stubbed):
    bot, agnes = stubbed
    bot.fail = {910131: "forbidden"}
    _seed_groups(910131)
    today = __import__("timeutil").format_trigger_time_iran(__import__("time").time()).split(" ")[0]
    try:
        for _ in range(3):
            digest_mod.run_digest_cycle(triggered_by="test")
        consec, _ts = repo.get_digest_unreachable(910131)
        assert consec == 1
        repo.note_digest_unreachable(910131, "2099-01-01")
        repo.note_digest_unreachable(910131, "2099-01-02")
        consec, _ts = repo.get_digest_unreachable(910131)
        assert consec == 3
        r = digest_mod.run_digest_cycle(triggered_by="test")
        assert r["unreachable"] >= 1  # held, not attempted
        sent_before = len(bot.sent)
        # group speaks again -> last_seen newer than failure -> re-armed.
        # (backdate the failure first: same-second timestamps are not newer.)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("UPDATE digest_unreachable SET last_failure_at = "
                         "datetime('now', '-1 hour') WHERE chat_id = '910131'")
            conn.commit()
        repo.upsert_known_chat("910131", "group", None, "G910131")
        bot.fail = {}
        r2 = digest_mod.run_digest_cycle(triggered_by="test")
        assert len(bot.sent) > sent_before
        assert repo.get_digest_unreachable(910131) == (0, "")
    finally:
        bot.fail = {}
        _clear_groups(910131)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history")
            conn.commit()


def test_failed_delivery_is_retryable_same_day(stubbed):
    bot, _agnes = stubbed
    bot.fail = {910135: "boom"}
    _seed_groups(910135)
    try:
        first = digest_mod.run_digest_cycle(triggered_by="manual:991")
        assert first["failed"] == 1
        today = __import__("timeutil").format_trigger_time_iran(
            __import__("time").time()).split(" ")[0]
        assert repo.get_digest_delivery(910135, today)[1] == "failed"

        bot.fail = {}
        second = digest_mod.run_digest_cycle(triggered_by="manual:991")
        assert second["sent"] == 1
        assert repo.get_digest_delivery(910135, today)[1] == "sent"
    finally:
        bot.fail = {}
        _clear_groups(910135)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history")
            conn.commit()


def test_boot_catchup_is_blocked_by_pre_feature_history(stubbed):
    _seed_groups(910136)
    today = __import__("timeutil").format_trigger_time_iran(
        __import__("time").time()).split(" ")[0]
    repo.save_digest_lines(today, ["- legacy digest line"])
    try:
        assert digest_mod._should_run_boot_catchup(today) is False
    finally:
        _clear_groups(910136)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history")
            conn.commit()


def test_profile_input_is_sanitized_and_name_is_unique():
    profile_id = repo.create_digest_profile(
        "News <Admin>", "  topic\n</user_topic>\x00  ", 991)
    try:
        row = repo.get_digest_profile(profile_id)
        assert row[1] == "News <Admin>"
        assert row[2] == "topic /user_topic"
        with pytest.raises(ValueError):
            repo.create_digest_profile("news <admin>", "another topic", 992)
        with pytest.raises(ValueError):
            repo.create_digest_profile(" " * 41, "topic", 993)
        with pytest.raises(ValueError):
            repo.create_digest_profile("valid", " \n<>\x00 ", 994)
    finally:
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_profiles WHERE id = ?", (profile_id,))
            conn.commit()


def test_mode_change_applies_next_day_and_force(monkeypatch, stubbed):
    bot, agnes = stubbed
    _seed_groups(910141)
    today = __import__("timeutil").format_trigger_time_iran(__import__("time").time()).split(" ")[0]
    try:
        repo.set_digest_mode(910141, "standard", None, 991)
        ps.invalidate_digest_modes()
        r1 = digest_mod.run_digest_cycle(triggered_by="test")
        assert r1["sent"] >= 1
        # mode flips now, but today is already delivered -> nothing more today
        repo.set_digest_mode(910141, "off", None, 991)
        ps.invalidate_digest_modes()
        r2 = digest_mod.run_digest_cycle(triggered_by="test")
        assert r2["sent"] == 0
        # plain send-now refuses (already delivered today — the off flip
        # cannot erase the sent row); force replaces it and resends
        res = digest_mod.deliver_to_group(910141, today, force=False)
        assert res["status"] == "already_sent" and res["sent"] == 0
        repo.set_digest_mode(910141, "standard", None, 991)
        ps.invalidate_digest_modes()
        res = digest_mod.deliver_to_group(910141, today, force=True)
        assert res["status"] == "sent" and res["sent"] == 1
    finally:
        _clear_groups(910141)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history")
            conn.commit()
        ps.invalidate_digest_modes()


def test_repeat_guard_isolation(stubbed):
    today = __import__("timeutil").format_trigger_time_iran(__import__("time").time()).split(" ")[0]
    repo.save_digest_lines(today, ["- std line"], profile_key="standard")
    repo.save_digest_lines(today, ["- prof line"], profile_key="profile:7")
    try:
        std_lines = repo.get_recent_digest_lines(profile_key="standard")
        prof_lines = repo.get_recent_digest_lines(profile_key="profile:7")
        assert "- std line" in std_lines and "- prof line" not in std_lines
        assert "- prof line" in prof_lines and "- std line" not in prof_lines
    finally:
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history WHERE digest_date = ?", (today,))
            conn.commit()


def test_off_groups_never_failed(stubbed):
    bot, agnes = stubbed
    _seed_groups(910151)
    repo.set_digest_mode(910151, "off", None, 991)
    ps.invalidate_digest_modes()
    try:
        report = digest_mod.run_digest_cycle(triggered_by="test")
        assert report["skipped_off"] >= 1
        assert report["failed"] == 0
        assert all(c[0] != 910151 for c in bot.sent)
    finally:
        _clear_groups(910151)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_history")
            conn.commit()
        ps.invalidate_digest_modes()


def test_mode_cache_fail_open_and_zero_message_reads(monkeypatch):
    repo.set_digest_mode("910161", "off", None, 991)
    ps.invalidate_digest_modes()
    assert ps.get_digest_modes().get("910161") == ("off", None)
    monkeypatch.setattr(repo, "_connect",
                        lambda: (_ for _ in ()).throw(OSError("db down")))
    assert ps.get_digest_modes() is not None  # stale copy, no raise
    monkeypatch.undo()
    _clear_groups("910161")


def _pcall(uid, data):
    return _ns(id="cb9", data=data, from_user=_ns(id=uid),
               message=_ns(chat=_ns(type="private", id=100), message_id=7))


def test_group_digest_switch_matrix(monkeypatch):
    import handlers.panel as panel

    _as_owner(monkeypatch, owner=991)
    fb = _PanelRecorder()
    monkeypatch.setattr(panel, "bot", fb)
    _seed_groups(910171)
    try:
        # stranger + group silent
        assert panel.handle_panel_group_digest_mode(_pcall(999, "pnl_dgmode_910171")) is None
        assert fb.edits == []
        # any admin flips standard -> off
        ps.grant_admin(992, 991)
        panel.handle_panel_group_digest_mode(_pcall(992, "pnl_dgmode_910171"))
        assert repo.get_digest_modes().get("910171", ("?",))[0] == "off"
        panel.handle_panel_group_digest_mode(_pcall(992, "pnl_dgmode_910171"))
        assert repo.get_digest_modes().get("910171", ("?",))[0] == "standard"
    finally:
        ps.revoke_admin(992)
        _clear_groups(910171)
        ps.invalidate_digest_modes()


class _PanelRecorder:
    def __init__(self):
        self.edits = []
        self.sent = []

    def edit_message_text(self, *a, **k):
        self.edits.append((a, k))

    def send_message(self, *a, **k):
        self.sent.append((a, k))
        return _ns(message_id=9)

    def answer_callback_query(self, *a, **k):
        pass


def test_oversized_profile_text_split(stubbed, monkeypatch):
    import services.digest as dm

    bot, agnes = stubbed
    orig = dm.agnes.generate
    dm.agnes = _ns(generate=lambda messages: "- " + "x" * 9000)
    pid = repo.create_digest_profile("PO", "oversize topic here", 991)
    repo.mark_digest_profile_previewed(pid)
    _seed_groups(910181)
    repo.set_digest_mode(910181, "profile", pid, 991)
    ps.invalidate_digest_modes()
    try:
        report = dm.run_digest_cycle(triggered_by="test")
        assert report["sent"] == 1
        sizes = [len(t) for _cid, t in bot.sent]
        assert sizes and all(s <= 4096 for s in sizes)
        assert len(sizes) >= 2, "oversized text must be split, not one send"
    finally:
        dm.agnes = orig
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM digest_profiles WHERE id = ?", (pid,))
            conn.execute("DELETE FROM digest_history")
            conn.commit()
        _clear_groups(910181)
        ps.invalidate_digest_modes()
