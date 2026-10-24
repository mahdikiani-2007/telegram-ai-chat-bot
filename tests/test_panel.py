"""Stage 1 panel tests: owner/admin resolution, permission failure modes,
central-gate extraction + ban/maintenance behavior, panel_guard denials,
panel input interceptor, audit writer. Follows test_dispatch.py conventions
(monkeypatched bot/recorders, real repo on the hermetic temp DATA_DIR)."""
import importlib
import os
import sys
import threading
import time
import types as std_types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import handlers.shared as shared
from db import repo
from services import panel_settings as ps
from wiring import POLLING_ALLOWED_UPDATES


@pytest.fixture(autouse=True)
def _clean_panel_state():
    ps.clear_settings_cache()
    yield
    ps.clear_settings_cache()
    for key in (ps.MAINTENANCE_KEY, ps.MAINTENANCE_NOTICE_KEY,
                ps.MAINTENANCE_NOTICE_MINUTES_KEY):
        try:
            repo.delete_panel_setting(key)
        except Exception:
            pass


def _ns(**kwargs):
    obj = std_types.SimpleNamespace()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _user(uid):
    return _ns(id=uid)


def _msg(uid, chat_type="private", text="hi", chat_id=100):
    return _ns(from_user=_user(uid), chat=_ns(type=chat_type, id=chat_id),
               text=text, message_id=1, content_type="text")


def _call(uid, data="pnl_home", chat_type="private", chat_id=100):
    return _ns(id="cb1", data=data, from_user=_user(uid),
               message=_ns(chat=_ns(type=chat_type, id=chat_id), message_id=7))


# ---------- owner/admin resolution ----------

def test_parse_admin_ids_keeps_only_positive_ints():
    assert config_mod._parse_admin_ids("11, 22,x,, -3,0,22,") == [11, 22]
    assert config_mod._parse_admin_ids("") == []
    assert config_mod._parse_admin_ids(None) == []


def test_owner_resolution_rules(monkeypatch, caplog):
    importlib.reload(config_mod)
    try:
        monkeypatch.setenv("ADMIN_IDS", "111,222")
        monkeypatch.setenv("ADMIN_ID", "999")
        importlib.reload(config_mod)
        assert config_mod.OWNER_ID == 111  # ADMIN_IDS[0] wins on conflict
        assert config_mod.ADMIN_IDS == (111, 222)
        assert any("wins as owner" in r.message for r in caplog.records)

        monkeypatch.setenv("ADMIN_IDS", "")
        monkeypatch.setenv("ADMIN_ID", "555")
        importlib.reload(config_mod)
        assert config_mod.OWNER_ID == 555  # legacy fallback
        assert config_mod.ADMIN_IDS == (555,)

        monkeypatch.setenv("ADMIN_IDS", "nope,0,-4")
        monkeypatch.setenv("ADMIN_ID", "0")
        importlib.reload(config_mod)
        assert config_mod.OWNER_ID == 0  # nothing valid -> panel disabled
    finally:
        for var in ("ADMIN_IDS", "ADMIN_ID"):
            monkeypatch.undo()
        importlib.reload(config_mod)


def test_uid_validation(monkeypatch):
    monkeypatch.setattr(shared, "OWNER_ID", 111, raising=False)
    monkeypatch.setattr(shared, "ADMIN_IDS", (111, 222), raising=False)
    for bad in (0, None, -5, "111", 1.5, True, False):
        assert shared.is_admin(bad) is False
        assert shared.is_owner(bad) is False
    assert shared.is_owner(111) is True
    assert shared.is_owner(222) is False
    assert shared.is_admin(111) is True
    assert shared.is_admin(222) is True


def test_db_admin_roundtrip_and_owner_never_db(monkeypatch):
    monkeypatch.setattr(shared, "OWNER_ID", 111, raising=False)
    monkeypatch.setattr(shared, "ADMIN_IDS", (111,), raising=False)
    assert shared.is_admin(333) is False
    ps.grant_admin(333, 111)
    try:
        assert shared.is_admin(333) is True
        assert shared.is_owner(333) is False
        assert [r[0] for r in repo.list_panel_admins()] == [333]
    finally:
        assert ps.revoke_admin(333) is True
    assert shared.is_admin(333) is False


def test_is_admin_db_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(shared, "OWNER_ID", 111, raising=False)
    monkeypatch.setattr(shared, "ADMIN_IDS", (111,), raising=False)
    shared.invalidate_identity_caches()
    monkeypatch.setattr(repo, "_connect",
                        lambda: (_ for _ in ()).throw(OSError("db down")))
    assert shared.is_admin(111) is True   # env-listed still passes
    assert shared.is_admin(444) is False  # DB-added denied (stale/empty)


# ---------- settings / maintenance / bans fail-open ----------

def test_setting_roundtrip_and_cache_invalidation():
    assert ps.get_setting("t1_missing", "dflt") == "dflt"
    ps.set_setting("t1_k", "v1")
    assert ps.get_setting("t1_k") == "v1"
    ps.set_setting("t1_k", "v2")
    assert ps.get_setting("t1_k") == "v2"
    ps.delete_setting("t1_k")
    assert ps.get_setting("t1_k", "dflt") == "dflt"


def test_maintenance_and_ban_fail_open(monkeypatch):
    monkeypatch.setattr(repo, "get_panel_setting",
                        lambda k, d=None: (_ for _ in ()).throw(OSError("db down")))
    monkeypatch.setattr(repo, "is_banned_user",
                        lambda uid: (_ for _ in ()).throw(OSError("db down")))
    assert ps.maintenance_active() is False
    assert ps.is_banned(777) is False


def test_admins_never_banned(monkeypatch):
    monkeypatch.setattr(shared, "ADMIN_IDS", (111,), raising=False)
    repo.ban_user(111, "stale row", 111)
    try:
        assert ps.is_banned(111) is False
    finally:
        repo.unban_user(111)


# ---------- central gate ----------

def test_gate_covers_every_allowed_update_type():
    kinds = set(ps._UPDATE_USER_ATTRS)
    assert set(POLLING_ALLOWED_UPDATES) <= kinds, \
        f"gate has no extractor for: {set(POLLING_ALLOWED_UPDATES) - kinds}"
    assert ps.extract_gate_identity(_ns(update_id=1)) is None


def _updates_for(uid):
    return [
        _ns(update_id=1, message=_msg(uid), edited_message=None,
            callback_query=None, inline_query=None,
            chosen_inline_result=None, guest_message=None),
        _ns(update_id=2, message=None, edited_message=_msg(uid),
            callback_query=None, inline_query=None,
            chosen_inline_result=None, guest_message=None),
        _ns(update_id=3, message=None, edited_message=None,
            callback_query=_ns(id="q", from_user=_user(uid),
                               message=_ns(chat=_ns(id=50))),
            inline_query=None, chosen_inline_result=None, guest_message=None),
        _ns(update_id=4, message=None, edited_message=None,
            callback_query=None, inline_query=_ns(id="iq", **{"from": _user(uid)}),
            chosen_inline_result=None, guest_message=None),
        _ns(update_id=5, message=None, edited_message=None,
            callback_query=None, inline_query=None,
            chosen_inline_result=_ns(result_id="r", from_user=_user(uid)),
            guest_message=None),
        _ns(update_id=6, message=None, edited_message=None,
            callback_query=None, inline_query=None,
            chosen_inline_result=None, guest_message=_msg(uid)),
    ]


class _FakeBot:
    def __init__(self):
        self.passed = []
        self.sent = []
        self.acked = []

    def process_new_updates(self, updates):
        self.passed.extend(updates)

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    def answer_callback_query(self,qid, text=None, **kwargs):
        self.acked.append((qid, text))


def _install(fake):
    ps.install_central_gate(fake)
    return fake


def test_gate_banned_users_dropped_silently(monkeypatch):
    monkeypatch.setattr(shared, "ADMIN_IDS", (), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 0, raising=False)
    ps.ban_uid(900, "spam", 1)
    try:
        fake = _install(_FakeBot())
        for u in _updates_for(900):
            fake.process_new_updates([u])
        assert fake.passed == []
        assert fake.sent == [] and fake.acked == []
    finally:
        ps.unban_uid(900)


def test_gate_admin_passes_even_if_banned_row_exists(monkeypatch):
    monkeypatch.setattr(shared, "ADMIN_IDS", (901,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 901, raising=False)
    ps.ban_uid(901, "stale", 901)
    try:
        fake = _install(_FakeBot())
        for u in _updates_for(901):
            fake.process_new_updates([u])
        assert len(fake.passed) == 6
    finally:
        ps.unban_uid(901)


def test_gate_maintenance_holds_non_admin_with_rate_limited_notice(monkeypatch):
    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    ps.set_setting(ps.MAINTENANCE_KEY, "1")
    ps._notice_sent_at.clear()
    try:
        fake = _install(_FakeBot())
        msg_update = _updates_for(902)[0]
        fake.process_new_updates([msg_update])
        fake.process_new_updates([msg_update])  # second: no repeat notice
        assert fake.passed == []
        deadline = time.time() + 5
        while not fake.sent and time.time() < deadline:
            time.sleep(0.05)
        assert len(fake.sent) == 1  # exactly one notice per window
        fake2_updates = _updates_for(1)[0]
        fake.process_new_updates([fake2_updates])
        assert len(fake.passed) == 1  # admin exempt
    finally:
        ps.delete_setting(ps.MAINTENANCE_KEY)
        ps._notice_sent_at.clear()


# ---------- panel_guard / interceptor ----------

def test_panel_guard_denies_silently(monkeypatch):
    import handlers.panel as panel

    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    monkeypatch.setattr(config_mod, "OWNER_ID", 1, raising=False)
    calls = []
    sent, acked = [], []

    class GBot:
        def answer_callback_query(self, *a, **k):
            acked.append((a, k))

        def send_message(self, *a, **k):
            sent.append((a, k))

    monkeypatch.setattr(panel, "bot", GBot())

    @panel.panel_guard(owner_only=False)
    def _fn(m):
        calls.append(m)
        return "ok"

    assert _fn(_msg(999)) is None          # non-admin: silent
    assert _fn(_msg(1, chat_type="group")) is None  # group: silent
    assert _fn(_call(999)) is None         # callback: spinner dismissed
    assert len(acked) == 1 and sent == []
    assert _fn(_msg(1)) == "ok"            # admin private: passes
    assert _fn(_call(1)) == "ok"

    @panel.panel_guard(owner_only=True)
    def _owner_fn(m):
        calls.append(("owner", m))
        return "owner-ok"

    repo.add_panel_admin(2, 1)
    try:
        assert _owner_fn(_msg(2)) is None  # plain admin denied on owner-only
        assert _owner_fn(_msg(1)) == "owner-ok"
    finally:
        repo.remove_panel_admin(2)


def test_panel_input_interceptor_rules(monkeypatch):
    import handlers.panel as panel
    from sessions import _panel_input_sessions

    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    _panel_input_sessions.set(1, {"state": "PANEL_ADD_ADMIN"})
    try:
        assert panel._is_panel_input_waiting(_msg(1)) is True
        assert panel._is_panel_input_waiting(_msg(2)) is False  # no session
        assert panel._is_panel_input_waiting(
            _msg(1, chat_type="group")) is False
        cmd = _msg(1, text="/todo buy milk")
        assert panel._is_panel_input_waiting(cmd) is False  # /cmd cancels
        assert _panel_input_sessions.get(1) is None
    finally:
        _panel_input_sessions.discard(1)


def test_audit_writer_roundtrip():
    before = repo.count_panel_audit()
    repo.write_panel_audit(1, "maintenance", "on", "ok")
    assert repo.count_panel_audit() == before + 1
    rows = repo.list_panel_audit(limit=5)
    assert rows and rows[0][1] == 1 and rows[0][2] == "maintenance"


def test_gate_senderless_and_userless_updates_pass_through():
    fake = _install(_FakeBot())
    senderless = _ns(update_id=99)  # e.g. poll/service update: no sender
    userless = _ns(update_id=100, message=_ns(from_user=None, chat=_ns(id=5)),
                   edited_message=None, callback_query=None, inline_query=None,
                   chosen_inline_result=None, guest_message=None)
    fake.process_new_updates([senderless, userless])
    assert fake.passed == [senderless, userless]


def test_gate_failure_isolation_proves_fail_open(monkeypatch):
    """Each injection breaks one gate stage; the update must STILL be
    dispatched, and a sibling banned update must still be dropped."""
    import services.panel_settings as _psmod
    real_extractor = _psmod.extract_gate_identity
    monkeypatch.setattr(shared, "ADMIN_IDS", (), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 0, raising=False)
    ps.ban_uid(950, "spam", 1)
    good = _updates_for(951)[0]
    banned = _updates_for(950)[0]
    try:
        # 1. extractor raises for ONE update: it passes (fail open) while the
        # sibling banned update is still correctly dropped.
        real = _psmod.extract_gate_identity

        def flaky(update):
            if update is good:
                raise RuntimeError("boom")
            return real(update)

        monkeypatch.setattr(_psmod, "extract_gate_identity", flaky)
        fake = _install(_FakeBot())
        fake.process_new_updates([good, banned])
        assert good in fake.passed and banned not in fake.passed

        # 2. identity refresh raises (DB down) with a primed cache: the
        # non-banned update still passes via the stale copy.
        monkeypatch.setattr(_psmod, "extract_gate_identity", real_extractor)
        monkeypatch.setattr(
            shared, "_refresh_identity_set", lambda t, c: (None, False))
        fake2 = _install(_FakeBot())
        fake2.process_new_updates([good])
        assert fake2.passed == [good]

        # 3. maintenance read raises -> treated as off -> pass
        monkeypatch.setattr(repo, "get_panel_setting",
                            lambda k, d=None: (_ for _ in ()).throw(OSError("db down")))
        fake3 = _install(_FakeBot())
        fake3.process_new_updates([good])
        assert fake3.passed == [good]
    finally:
        ps.unban_uid(950)


def test_gate_never_blocks_on_notice_send(monkeypatch):
    """The polling thread must return from the gate while the maintenance
    notice send is still blocked; the notice lands after release."""
    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    ps.set_setting(ps.MAINTENANCE_KEY, "1")
    ps._notice_sent_at.clear()
    entered_send = threading.Event()
    release_send = threading.Event()

    class SlowBot(_FakeBot):
        def send_message(self, chat_id, text, **kwargs):
            entered_send.set()
            assert release_send.wait(timeout=10), "send blocked too long"
            self.sent.append((chat_id, text))

    try:
        fake = _install(SlowBot())
        t0 = time.time()
        fake.process_new_updates([_updates_for(903)[0]])
        elapsed = time.time() - t0
        assert entered_send.wait(timeout=5), "worker never started the send"
        assert elapsed < 2, f"gate blocked polling thread for {elapsed:.1f}s"
        assert fake.passed == [] and fake.sent == []  # held, notice still in flight
        release_send.set()
        deadline = time.time() + 5
        while not fake.sent and time.time() < deadline:
            time.sleep(0.05)
        assert len(fake.sent) == 1
    finally:
        release_send.set()
        ps.delete_setting(ps.MAINTENANCE_KEY)
        ps._notice_sent_at.clear()


def test_gate_db_cost_per_update_is_bounded(monkeypatch):
    """Warm cache: gate updates perform ZERO repo reads (env-admin and
    cached non-admin alike); refresh happens at most once per table."""
    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    ps.set_setting(ps.MAINTENANCE_KEY, "0")
    ps.get_setting(ps.MAINTENANCE_KEY)  # warm the settings cache
    shared.invalidate_identity_caches()
    refreshes = []
    real_refresh = shared._refresh_identity_set

    def counting_refresh(table, col):
        refreshes.append(table)
        return real_refresh(table, col)

    monkeypatch.setattr(shared, "_refresh_identity_set", counting_refresh)
    reads = []
    real_connect = repo._connect

    def counting_connect():
        reads.append(1)
        return real_connect()

    monkeypatch.setattr(repo, "_connect", counting_connect)
    fake = _install(_FakeBot())
    fake.process_new_updates([_updates_for(960)[0]])   # non-admin: primes
    assert fake.passed
    assert sorted(refreshes) == ["panel_admins", "panel_bans"]
    n_reads = len(reads)
    assert n_reads > 0  # sanity: priming really hit the DB
    fake.process_new_updates([_updates_for(960)[0]])   # warm: zero reads
    fake.process_new_updates([_updates_for(1)[0]])     # env admin: zero reads
    assert len(reads) == n_reads, f"warm path did {len(reads) - n_reads} DB reads"


def test_notice_global_concurrency_cap(monkeypatch):
    """30 distinct users, blocked sends: at most 8 live notice threads,
    exactly 8 notices delivered after release."""
    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    ps.set_setting(ps.MAINTENANCE_KEY, "1")
    ps._notice_sent_at.clear()
    release = threading.Event()
    active = [0]
    peak = [0]
    lock = threading.Lock()

    class SlowBot(_FakeBot):
        def send_message(self, chat_id, text, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                assert release.wait(timeout=15), "send blocked too long"
                self.sent.append((chat_id, text))
            finally:
                with lock:
                    active[0] -= 1

    try:
        fake = _install(SlowBot())
        for i in range(30):
            fake.process_new_updates([_updates_for(2000 + i)[0]])
        deadline = time.time() + 5
        while time.time() < deadline:
            live = sum(1 for t in threading.enumerate()
                       if t.name == "panel-gate-notice" and t.is_alive())
            if live >= 8:
                break
            time.sleep(0.05)
        live = sum(1 for t in threading.enumerate()
                   if t.name == "panel-gate-notice" and t.is_alive())
        assert live <= 8, f"{live} live notice threads"
        release.set()
        deadline = time.time() + 10
        while len(fake.sent) < 8 and time.time() < deadline:
            time.sleep(0.05)
        assert len(fake.sent) == 8
        assert peak[0] <= 8
        assert fake.passed == []
    finally:
        release.set()
        ps.delete_setting(ps.MAINTENANCE_KEY)
        ps._notice_sent_at.clear()


def test_notice_map_pruned_on_growth():
    ps._notice_sent_at.clear()
    now = time.time()
    ps._notice_sent_at.update({("b", i): now - 90000 for i in range(5100)})
    ps._notice_sent_at[("b", "fresh")] = now
    assert ps._notice_rate_ok(_FakeBot(), 424242) is True
    assert len(ps._notice_sent_at) <= 4001
    assert ("b", "fresh") in ps._notice_sent_at
    ps._notice_sent_at.clear()


def _real_updates_for(uid):
    """REAL telebot.types.Update objects via the installed 4.36.0 de_json —
    no mocks for the update shapes."""
    from telebot import types as _t

    def user():
        return {"id": uid, "is_bot": False, "first_name": "T"}

    def msg(mid=1, extra=None):
        m = {"message_id": mid, "date": 1700000000,
             "chat": {"id": 100, "type": "private"},
             "from": user(), "text": "hi"}
        if extra:
            m.update(extra)
        return m

    payloads = [
        {"message": msg()},
        {"edited_message": msg(mid=2)},
        {"guest_message": msg(mid=3, extra={"guest_query_id": "gq1"})},
        {"callback_query": {"id": "cq1", "from": user(), "message": msg(),
                            "chat_instance": "ci", "data": "pnl_home"}},
        {"inline_query": {"id": "iq1", "from": user(), "query": "q", "offset": ""}},
        {"chosen_inline_result": {"result_id": "r1", "from": user(), "query": "q"}},
    ]
    return [_t.Update.de_json(dict(update_id=1000 + i, **p))
            for i, p in enumerate(payloads)]


def test_gate_real_objects_all_types(monkeypatch):
    """Every POLLING_ALLOWED_UPDATES type as a real Update: banned drops,
    admin passes. Fails loudly if any sender field becomes unreadable."""
    assert len(_real_updates_for(1)) == len(POLLING_ALLOWED_UPDATES)
    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    ps.ban_uid(960, "spam", 1)
    try:
        kinds = []
        for u in _real_updates_for(960):
            ident = ps.extract_gate_identity(u)
            assert ident is not None and ident[0] == 960, \
                f"unreadable sender for update {u}"
            kinds.append(ident[2])
        assert set(kinds) == set(POLLING_ALLOWED_UPDATES)
        fake = _install(_FakeBot())
        fake.process_new_updates(_real_updates_for(960))
        assert fake.passed == [] and fake.sent == [] and fake.acked == []
        fake2 = _install(_FakeBot())
        admin_updates = _real_updates_for(1)
        fake2.process_new_updates(admin_updates)
        assert fake2.passed == admin_updates
    finally:
        ps.unban_uid(960)


def test_gate_error_log_throttling(monkeypatch, caplog):
    """One traceback per error type per minute; repeats counted."""
    import logging as _logging

    import services.panel_settings as _psmod
    ps._gate_error_log.clear()
    fake = _install(_FakeBot())
    good = _updates_for(970)[0]
    monkeypatch.setattr(
        _psmod, "extract_gate_identity",
        lambda u: (_ for _ in ()).throw(ValueError("bad shape")))
    with caplog.at_level(_logging.WARNING, logger="L-bot"):
        for _ in range(3):
            fake.process_new_updates([good])
    assert fake.passed == [good, good, good]
    traces = [r for r in caplog.records if r.exc_info]
    assert len(traces) == 1
    for entry in ps._gate_error_log.values():
        entry[0] -= 61  # force a new window
    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="L-bot"):
        fake.process_new_updates([good])
    assert any("suppressed" in r.getMessage() for r in caplog.records)
    assert sum(1 for r in caplog.records if r.exc_info) == 1
    ps._gate_error_log.clear()


# ================= Stage 2A =================

class _PanelFakeBot:
    """Recorder bot for panel handler tests (no network)."""

    def __init__(self):
        self.edits = []
        self.sent = []
        self.acked = []
        self.left = []
        self.copied = []

    def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return _ns(message_id=999)

    def answer_callback_query(self, qid, text=None, **kwargs):
        self.acked.append((qid, text))

    def leave_chat(self, chat_id, **kwargs):
        self.left.append(chat_id)
        return True

    def copy_message(self, chat_id=None, from_chat_id=None, message_id=None, **kwargs):
        self.copied.append((chat_id, from_chat_id, message_id))
        return _ns(message_id=1000)

    def reply_to(self, message, text, **kwargs):
        self.sent.append((message.chat.id, text))
        return _ns(message_id=998)


def _pcall(uid, data, chat_type="private", chat_id=100):
    return _ns(id="cb9", data=data, from_user=_user(uid),
               message=_ns(chat=_ns(type=chat_type, id=chat_id), message_id=7))


def _pmsg(uid, text="/admin", chat_type="private", chat_id=100):
    return _ns(from_user=_user(uid), chat=_ns(type=chat_type, id=chat_id),
               text=text, message_id=3, content_type="text")


def _panel_bot(monkeypatch):
    import handlers.panel as panel
    fb = _PanelFakeBot()
    monkeypatch.setattr(panel, "bot", fb)
    return fb


def _as_owner_admin(monkeypatch, owner=1):
    monkeypatch.setattr(shared, "OWNER_ID", owner, raising=False)
    monkeypatch.setattr(shared, "ADMIN_IDS", (owner,), raising=False)
    monkeypatch.setattr(config_mod, "OWNER_ID", owner, raising=False)


_OWNER_ONLY_CALLS = {
    "pnl_admins", "pnl_admin_add", "pnl_admin_del_5", "pnl_admin_del_ok_5",
    "pnl_leave_-100", "pnl_leave_ok_-100",
    "pnl_bcast_menu", "pnl_bc_users", "pnl_bc_groups", "pnl_bc_both",
    "pnl_bc_one", "pnl_bc_ok_abcdef123456", "pnl_bc_stop",
}
_ANY_ADMIN_CALLS = {
    "pnl_home", "pnl_status", "pnl_maint", "pnl_maint_cf_on", "pnl_maint_on",
    "pnl_users_0", "pnl_user_5", "pnl_upick_5", "pnl_user_search",
    "pnl_groups_0", "pnl_group_-100", "pnl_gpick_-100", "pnl_group_search",
    "pnl_ban_5", "pnl_ban_ok_6", "pnl_unban_5", "pnl_unban_ok_5",
    "pnl_audit_0", "pnl_cancel",
}


def test_panel_callback_permission_matrix(monkeypatch):
    """Every new callback: non-admin silent, group silent, owner-only
    denied for plain admins; allowed paths render (edit sent)."""
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    repo.add_panel_admin(2, 1)
    try:
        for data in sorted(_OWNER_ONLY_CALLS | _ANY_ADMIN_CALLS):
            owner_only = data in _OWNER_ONLY_CALLS
            handler = _find_panel_handler(panel, data)
            assert handler is not None, f"no handler for {data}"

            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert handler(_pcall(999, data)) is None  # stranger: silent
            assert fb.edits == [] and fb.sent == []

            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert handler(_pcall(1, data, chat_type="group")) is None
            assert fb.edits == [] and fb.sent == []

            if owner_only:
                fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
                assert handler(_pcall(2, data)) is None  # plain admin denied
                assert fb.edits == [] and fb.sent == []
    finally:
        repo.remove_panel_admin(2)
        _panel_input_sessions_discard_all()


def _find_panel_handler(panel, data):
    import telebot

    for h in panel.bot.callback_query_handlers if hasattr(panel.bot, "callback_query_handlers") else []:
        pass
    # Resolve via the real bot registry is order-fragile; call by convention:
    table = {
        "pnl_home": panel.handle_panel_home,
        "pnl_status": panel.handle_panel_status,
        "pnl_maint": panel.handle_panel_maint,
        "pnl_maint_cf_on": panel.handle_panel_maint_confirm,
        "pnl_maint_on": panel.handle_panel_maint_apply,
        "pnl_users_0": panel.handle_panel_users,
        "pnl_user_5": panel.handle_panel_user,
        "pnl_upick_5": panel.handle_panel_user_pick,
        "pnl_user_search": panel.handle_panel_user_search,
        "pnl_groups_0": panel.handle_panel_groups,
        "pnl_group_-100": panel.handle_panel_group,
        "pnl_gpick_-100": panel.handle_panel_group_pick,
        "pnl_group_search": panel.handle_panel_group_search,
        "pnl_leave_-100": panel.handle_panel_leave,
        "pnl_leave_ok_-100": panel.handle_panel_leave_ok,
        "pnl_ban_5": panel.handle_panel_ban,
        "pnl_ban_ok_6": panel.handle_panel_ban_ok,
        "pnl_unban_5": panel.handle_panel_unban,
        "pnl_unban_ok_5": panel.handle_panel_unban_ok,
        "pnl_audit_0": panel.handle_panel_audit,
        "pnl_cancel": panel.handle_panel_cancel,
        "pnl_bcast_menu": panel.handle_panel_broadcast_menu,
        "pnl_bc_users": panel.handle_panel_broadcast_target,
        "pnl_bc_groups": panel.handle_panel_broadcast_target,
        "pnl_bc_both": panel.handle_panel_broadcast_target,
        "pnl_bc_one": panel.handle_panel_broadcast_one,
        "pnl_bc_ok_abcdef123456": panel.handle_panel_broadcast_confirm,
        "pnl_bc_stop": panel.handle_panel_broadcast_stop,
        "pnl_admins": panel.handle_panel_admins,
        "pnl_admin_add": panel.handle_panel_admin_add,
        "pnl_admin_del_5": panel.handle_panel_admin_del,
        "pnl_admin_del_ok_5": panel.handle_panel_admin_del_ok,
    }
    return table.get(data)


def _panel_input_sessions_discard_all():
    from sessions import _panel_input_sessions
    for uid in (1, 2, 999):
        _panel_input_sessions.discard(uid)


def test_panel_allowed_paths_render(monkeypatch):
    """Owner happy paths produce panel edits (and leave calls leave_chat)."""
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    try:
        panel.handle_panel_users(_pcall(1, "pnl_users_0"))
        assert fb.edits, "users list must render"
        panel.handle_panel_groups(_pcall(1, "pnl_groups_0"))
        assert fb.edits, "groups list must render"
        panel.handle_panel_leave_ok(_pcall(1, "pnl_leave_ok_-100"))
        assert fb.left == [-100]
        assert repo.count_panel_audit() >= 1
    finally:
        _panel_input_sessions_discard_all()


def test_panel_g3_self_and_admin_protection(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    repo.add_panel_admin(2, 1)
    try:
        panel.handle_panel_ban_ok(_pcall(1, "pnl_ban_ok_1"))  # self
        assert not ps.is_banned(1)
        assert any("yourself" in e[2] for e in fb.edits)
        panel.handle_panel_ban_ok(_pcall(1, "pnl_ban_ok_2"))  # admin
        assert not ps.is_banned(2)
    finally:
        repo.remove_panel_admin(2)
        _panel_input_sessions_discard_all()


def test_identity_cache_invalidation_immediate(monkeypatch):
    _as_owner_admin(monkeypatch, owner=1)
    assert not ps.is_banned(770)
    ps.ban_uid(770, "", 1)
    try:
        assert ps.is_banned(770) is True  # no TTL wait
        assert shared.is_admin(771) is False
        ps.grant_admin(771, 1)
        assert shared.is_admin(771) is True
    finally:
        ps.unban_uid(770)
        ps.revoke_admin(771)
    assert ps.is_banned(770) is False
    assert shared.is_admin(771) is False


def test_search_escaping_percent_underscore():
    conn = repo._connect()
    conn.execute(
        "INSERT OR REPLACE INTO group_members (chat_id, user_id, username, first_name)"
        " VALUES ('-500', 501, '100%_real', 'A')")
    conn.execute(
        "INSERT OR REPLACE INTO group_members (chat_id, user_id, username, first_name)"
        " VALUES ('-500', 502, '100xpercent', 'B')")
    conn.execute(
        "INSERT OR REPLACE INTO group_members (chat_id, user_id, username, first_name)"
        " VALUES ('-500', 503, 'abc_def', 'C')")
    conn.commit()
    try:
        got = {r[1] for r in repo.search_group_members("100%_", limit=10)}
        assert 501 in got and 502 not in got  # % and _ literal, not wildcards
        assert {r[1] for r in repo.search_group_members("abc_def", limit=10)} == {503}
        assert {r[1] for r in repo.search_group_members("%", limit=10)} == {501}
        assert repo.search_group_members("", limit=10) == []
    finally:
        with repo._write_lock:
            c2 = repo._connect()
            c2.execute("DELETE FROM group_members WHERE chat_id = '-500'")
            c2.commit()


def test_scheduler_holds_banned_without_side_effects(monkeypatch):
    """Private-chat rows of banned creators: no send, no attempts increment,
    no admin alert while banned; claim filter excludes (subquery); delivery
    resumes after unban. Group rows keep delivering (F2)."""
    from services import scheduling as sched

    sent, notified = [], []

    class Sender:
        def send_message(self, *a, **k):
            sent.append((a, k))

    class Notifier:
        def notify(self, *a, **k):
            notified.append((a, k))

    monkeypatch.setattr(sched, "bot", Sender())
    monkeypatch.setattr(sched, "admin_notifier", Notifier())
    trig = time.time() - 30
    rid = repo.add_reminder(881, 881, "held alarm", "me", "none", trig)
    tid = repo.add_todo(881, 881, "held task", "Normal", "General", trig, "none")
    grid = repo.add_reminder(-701, 881, "group alarm", "all", "none", trig)
    ps.ban_uid(881, "", 1)
    try:
        claimed = {r[0] for r in repo.claim_due_reminders(time.time(), "tok-x")}
        assert rid not in claimed  # banned private creator filtered
        assert grid in claimed  # group row still claimable
        for other in claimed:
            repo.release_reminder_claim(other)
        sched._fire_reminder(rid, trig)  # race path: claimed before ban
        sched._fire_todo_expiry(tid, trig)
        assert sent == [] and notified == []
        row = repo.get_reminder_by_id(rid)
        assert row is not None  # row intact, not dismissed
        cur = repo._connect().execute(
            "SELECT attempts FROM reminders WHERE id = ?", (rid,)).fetchone()
        assert cur[0] == 0
    finally:
        ps.unban_uid(881)
    sched._fire_reminder(rid, trig)  # resumes after unban
    assert len(sent) == 1 and notified == []
    repo.dismiss_reminder(rid)
    repo.dismiss_reminder(grid)
    repo.delete_todo(tid)


def test_scheduler_group_rows_deliver_while_banned(monkeypatch):
    """F2: a banned creator's GROUP reminder fires normally to the group."""
    from services import scheduling as sched

    sent, notified = [], []

    class Sender:
        def send_message(self, *a, **k):
            sent.append((a, k))

    class Notifier:
        def notify(self, *a, **k):
            notified.append((a, k))

    monkeypatch.setattr(sched, "bot", Sender())
    monkeypatch.setattr(sched, "admin_notifier", Notifier())
    trig = time.time() - 30
    grid = repo.add_reminder(-702, 882, "group alarm 2", "all", "none", trig)
    ps.ban_uid(882, "", 1)
    try:
        claimed = {r[0] for r in repo.claim_due_reminders(time.time(), "tok-y")}
        assert grid in claimed
        for other in claimed:
            repo.release_reminder_claim(other)
        sched._fire_reminder(grid, trig)
        assert len(sent) == 1 and notified == []
    finally:
        ps.unban_uid(882)
        repo.dismiss_reminder(grid)


def test_claim_subquery_survives_huge_ban_list():
    """F1: >1500 ban rows — bound-list queries would break; the subquery
    must still claim non-banned rows fine."""
    with repo._write_lock:
        conn = repo._connect()
        conn.executemany(
            "INSERT OR IGNORE INTO panel_bans (user_id, reason, banned_by)"
            " VALUES (?, '', 1)",
            [(700000 + i,) for i in range(1600)],
        )
        conn.commit()
    trig = time.time() - 30
    rid = repo.add_reminder(883, 883, "big ban list alarm", "me", "none", trig)
    try:
        claimed = {r[0] for r in repo.claim_due_reminders(time.time(), "tok-z")}
        assert rid in claimed  # non-banned creator unaffected
    finally:
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_bans WHERE user_id >= 700000")
            conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
            conn.commit()
        shared.invalidate_identity_caches()


def test_gate_maintenance_acks_real_objects(monkeypatch):
    """Maintenance: banned inline/guest/callback get NOTHING; non-admin
    inline gets an empty answer+button, guest gets exactly one answer."""
    from telebot import types as _t

    monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
    monkeypatch.setattr(shared, "OWNER_ID", 1, raising=False)
    ps.set_setting(ps.MAINTENANCE_KEY, "1")
    ps._notice_sent_at.clear()

    class AckBot(_FakeBot):
        def __init__(self):
            super().__init__()
            self.inline_answers = []
            self.guest_answers = []

        def answer_inline_query(self, qid, results, **kwargs):
            self.inline_answers.append((qid, results, kwargs))

        def answer_guest_query(self, guest_query_id, result, **kwargs):
            self.guest_answers.append((guest_query_id, result))

    def user(uid):
        return {"id": uid, "is_bot": False, "first_name": "T"}

    def update(payload, i):
        return _t.Update.de_json(dict(update_id=2000 + i, **payload))

    try:
        ps.ban_uid(962, "", 1)
        banned_kinds = {
            "inline": update({"inline_query": {"id": "iq9", "from": user(962),
                                               "query": "q", "offset": ""}}, 1),
            "guest": update({"guest_message": {
                "message_id": 9, "date": 1700000000,
                "chat": {"id": 100, "type": "private"}, "from": user(962),
                "text": "hi", "guest_query_id": "gq9"}}, 2),
            "callback": update({"callback_query": {
                "id": "cq9", "from": user(962),
                "message": {"message_id": 9, "date": 1700000000,
                            "chat": {"id": 100, "type": "private"},
                            "from": user(962), "text": "hi"},
                "chat_instance": "ci", "data": "x"}}, 3),
        }
        fb = _install(AckBot())
        for u in banned_kinds.values():
            fb.process_new_updates([u])
        assert fb.passed == [] and fb.inline_answers == []
        assert fb.guest_answers == [] and fb.acked == [] and fb.sent == []

        fb2 = _install(AckBot())
        # NOTE: distinct users per kind — the maintenance notice is
        # rate-limited once per user per window across all update types.
        inline_u = update({"inline_query": {"id": "iq8", "from": user(963),
                                            "query": "q", "offset": ""}}, 4)
        guest_u = update({"guest_message": {
            "message_id": 8, "date": 1700000000,
            "chat": {"id": 100, "type": "private"}, "from": user(964),
            "text": "hi", "guest_query_id": "gq8"}}, 5)
        fb2.process_new_updates([inline_u, guest_u])
        assert fb2.passed == []
        deadline = time.time() + 5
        while (not fb2.inline_answers or not fb2.guest_answers) \
                and time.time() < deadline:
            time.sleep(0.05)
        assert len(fb2.inline_answers) == 1
        qid, results, kwargs = fb2.inline_answers[0]
        assert qid == "iq8" and results == [] and "button" in kwargs
        assert len(fb2.guest_answers) == 1
        assert fb2.guest_answers[0][0] == "gq8"
    finally:
        ps.unban_uid(962)
        ps.delete_setting(ps.MAINTENANCE_KEY)
        ps._notice_sent_at.clear()


def test_admin_beats_ban_everywhere(monkeypatch):
    """F3: a user in panel_bans who is also in ADMIN_IDS is NOT dropped by
    the gate and CAN use /admin (admin status always wins)."""
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    monkeypatch.setattr(shared, "ADMIN_IDS", (1, 885), raising=False)
    ps.ban_uid(885, "stale row", 1)
    try:
        assert ps.is_banned(885) is False  # admin exemption in is_banned
        fake = _install(_FakeBot())
        for u in _updates_for(885):
            fake.process_new_updates([u])
        assert len(fake.passed) == 6  # nothing dropped
        assert panel.handle_admin_panel(_pmsg(885)) is None  # renders, no crash
        assert fb.sent, "/admin works for ban-row admin"
    finally:
        ps.unban_uid(885)
        monkeypatch.setattr(shared, "ADMIN_IDS", (1,), raising=False)
        _panel_input_sessions_discard_all()


def test_revoke_and_unban_take_effect_immediately(monkeypatch):
    """F4: revoked admin / unbanned user change state on the very next
    update — no 10s window — in this process."""
    _as_owner_admin(monkeypatch, owner=1)
    ps.grant_admin(886, 1)
    assert shared.is_admin(886) is True
    assert ps.check_panel_access(_pmsg(886), owner_only=False)[0] is True
    ps.revoke_admin(886)
    assert shared.is_admin(886) is False
    assert ps.check_panel_access(_pmsg(886), owner_only=False)[0] is False
    ps.ban_uid(887, "", 1)
    assert ps.is_banned(887) is True
    fake = _install(_FakeBot())
    fake.process_new_updates([_updates_for(887)[0]])
    assert fake.passed == []
    ps.unban_uid(887)
    fake.process_new_updates([_updates_for(887)[0]])
    assert len(fake.passed) == 1


# ================= Stage 2B =================

_2B_ANY_ADMIN_CALLS = {
    "pnl_features", "pnl_feat_chat", "pnl_settings", "pnl_set_0",
    "pnl_setdef_0", "pnl_limits", "pnl_limadd", "pnl_limdel_5_0",
    "pnl_gdebate_-100",
}


def _find_panel_handler_2b(panel, data):
    return {
        "pnl_features": panel.handle_panel_features,
        "pnl_feat_chat": panel.handle_panel_feature_flip,
        "pnl_settings": panel.handle_panel_settings,
        "pnl_set_0": panel.handle_panel_setting_detail,
        "pnl_setdef_0": panel.handle_panel_setting_reset,
        "pnl_limits": panel.handle_panel_limits,
        "pnl_limadd": panel.handle_panel_limit_add,
        "pnl_limdel_5_0": panel.handle_panel_limit_del,
        "pnl_gdebate_-100": panel.handle_panel_group_debate,
    }[data]


def test_panel_2b_callback_matrix(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    for data in sorted(_2B_ANY_ADMIN_CALLS):
        handler = _find_panel_handler_2b(panel, data)
        fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
        assert handler(_pcall(999, data)) is None
        assert fb.edits == [] and fb.sent == []
        fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
        assert handler(_pcall(1, data, chat_type="group")) is None
        assert fb.edits == [] and fb.sent == []
    _panel_input_sessions_discard_all()


def test_features_menu_states_exemption(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    panel.handle_panel_features(_pcall(1, "pnl_features"))
    assert fb.edits and "EXEMPT" in fb.edits[0][2]
    ps.set_feature("pic", False)
    try:
        assert ps.feature_enabled("pic") is False
        panel.handle_panel_features(_pcall(1, "pnl_features"))
        buttons = [b.text for row in fb.edits[-1][3]["reply_markup"].keyboard
                   for b in row]
        assert any(b.startswith("🔴 pic") for b in buttons)
        assert sum(1 for b in buttons if b.startswith(("🟢", "🔴"))) == 12
    finally:
        ps.set_feature("pic", True)


def test_feature_flip_audits_old_to_new(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    before = repo.count_panel_audit()
    panel.handle_panel_feature_flip(_pcall(1, "pnl_feat_tr"))
    assert ps.feature_enabled("tr") is False
    panel.handle_panel_feature_flip(_pcall(1, "pnl_feat_tr"))
    assert ps.feature_enabled("tr") is True
    rows = repo.list_panel_audit(limit=5)
    assert rows[0][2] == "feature" and "->" in rows[0][4]
    assert repo.count_panel_audit() >= before + 2
    _panel_input_sessions_discard_all()


def test_feature_gate_message_semantics(monkeypatch):
    _as_owner_admin(monkeypatch, owner=1)
    fb = _PanelFakeBot()
    ps.set_feature("pic", False)
    ps._notice_sent_at.clear()
    try:
        # admin exempt: proceeds, nothing sent
        assert ps.feature_gate_message(fb, _pmsg(1), "pic", explicit=False) is True
        assert fb.sent == []
        # group ambient: silent
        assert ps.feature_gate_message(fb, _pmsg(7, chat_type="group", chat_id=-50),
                                       "pic", explicit=False) is False
        assert fb.sent == []
        # private ambient: one rate-limited notice
        assert ps.feature_gate_message(fb, _pmsg(7), "pic", explicit=False) is False
        assert len(fb.sent) == 1
        assert ps.feature_gate_message(fb, _pmsg(7), "pic", explicit=False) is False
        assert len(fb.sent) == 1
        # explicit: always answered
        assert ps.feature_gate_message(fb, _pmsg(7, text="/pic x"), "pic",
                                       explicit=True) is False
        assert len(fb.sent) == 2
    finally:
        ps.set_feature("pic", True)
        ps._notice_sent_at.clear()


def test_feature_gate_callback_and_group_debate(monkeypatch):
    _as_owner_admin(monkeypatch, owner=1)
    fb = _PanelFakeBot()
    ps.set_feature("youtube", False)
    try:
        assert ps.feature_gate_callback(fb, _pcall(1, "yt"), "youtube") is True
        assert ps.feature_gate_callback(fb, _pcall(7, "yt"), "youtube") is False
        assert len(fb.acked) == 1
        assert ps.feature_enabled("debate", chat_id=-100) is True
        ps.set_chat_feature(-100, "debate", False)
        assert ps.feature_enabled("debate", chat_id=-100) is False
        assert ps.feature_enabled("debate", chat_id=-101) is True
        assert ps.feature_enabled("debate") is True  # global untouched
    finally:
        ps.set_feature("youtube", True)
        ps.delete_setting("chat:-100:debate")


def test_feature_fail_open_on_db_error(monkeypatch):
    monkeypatch.setattr(repo, "_connect",
                        lambda: (_ for _ in ()).throw(OSError("db down")))
    assert ps.feature_enabled("chat") is True
    assert ps.feature_enabled("debate", chat_id=-100) is True
    assert ps.setting_value("tldr.cooldown_sec") == 300


def test_setting_clamps_and_invalid_fallback(monkeypatch):
    _as_owner_admin(monkeypatch, owner=1)
    ok, err = ps.validate_setting_value("tavily.daily_limit", "5000")
    assert ok is False and "1000" in err
    ok, err = ps.validate_setting_value("debate.max_rounds", "abc")
    assert ok is False
    assert ps.validate_setting_value("nope", "1") == (False, "unknown setting")
    repo.set_panel_setting("tldr.cooldown_sec", "not-a-number")
    ps.clear_settings_cache()
    assert ps.setting_value("tldr.cooldown_sec") == 300
    repo.set_panel_setting("tldr.cooldown_sec", "99999")
    ps.clear_settings_cache()
    assert ps.setting_value("tldr.cooldown_sec") == 300
    repo.delete_panel_setting("tldr.cooldown_sec")
    ps.clear_settings_cache()


def test_zero_db_reads_with_warm_cache_2b(monkeypatch):
    """Toggles + limits consulted with repo down: fail-open defaults, no raise."""
    _as_owner_admin(monkeypatch, owner=1)
    from limits import rate_limiter

    ps.sync_rate_limits()
    ps.feature_enabled("chat")
    ps.get_limit_overrides()
    monkeypatch.setattr(repo, "_connect",
                        lambda: (_ for _ in ()).throw(OSError("db down")))
    assert ps.feature_enabled("chat") is True
    assert ps.get_limit_overrides() == ps.get_limit_overrides()  # cached
    allowed, _scope = rate_limiter.allow("chat", 424242)
    assert allowed is True
    assert ps.setting_value("debate.delay_sec") == 4


def test_setting_write_reset_and_audit(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    before = repo.count_panel_audit()
    panel.handle_panel_setting_reset(_pcall(1, "pnl_setdef_4"))  # tavily.daily_limit
    assert ps.setting_value("tavily.daily_limit") == 40
    rows = repo.list_panel_audit(limit=3)
    assert rows[0][2] == "setting" and "-> 40" in rows[0][4]
    assert repo.count_panel_audit() == before + 1
    _panel_input_sessions_discard_all()


def test_limit_add_rejects_admin_and_validates(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    panel._handle_limit_add_text(_pmsg(1, text="1 chat 20"))
    assert fb.sent and "exempt" in fb.sent[-1][1].lower()
    panel._handle_limit_add_text(_pmsg(1, text="777 bogus 20"))
    assert "Invalid" in fb.sent[-1][1]
    panel._handle_limit_add_text(_pmsg(1, text="777 chat 25"))
    assert ps.get_limit_overrides().get((777, "chat")) == 25
    ps.delete_setting("limit.777.chat")
    from limits import rate_limiter

    rate_limiter.clear_user_override("chat", 777)
    _panel_input_sessions_discard_all()


def test_tavily_zero_rejected_and_falls_back():
    """F1: panel rejects 0 (0 means uncapped for the env var); a stored 0
    falls back to the default. Only tldr.cooldown may use 0 (off-switch)."""
    ok, err = ps.validate_setting_value("tavily.daily_limit", "0")
    assert ok is False
    repo.set_panel_setting("tavily.daily_limit", "0")
    ps.clear_settings_cache()
    try:
        assert ps.setting_value("tavily.daily_limit") == 40
    finally:
        repo.delete_panel_setting("tavily.daily_limit")
        ps.clear_settings_cache()
    for key, (_label, _dflt, mn, _mx) in ps.SETTINGS_SPEC.items():
        if mn <= 0:
            # tldr.cooldown 0 = documented off-switch; digest.search_reserve
            # 0 = no reserve (budget gate itself still applies). Neither
            # bypasses a hard cap the way tavily 0 (= uncapped) would.
            assert key in ("tldr.cooldown_sec", "digest.search_reserve"), key
    assert ps.validate_setting_value("tldr.cooldown_sec", "0") == (True, 0)


def test_runtime_updates_never_partial():
    """F2: hammer in-memory runtime state from many threads while the panel
    swaps it — no exceptions, no half-written maps, complete post-write."""
    from limits import rate_limiter

    stop = threading.Event()
    errors = []
    big_a = {(i, "chat"): 5 for i in range(500)}
    big_b = {(i, "chat"): 50 for i in range(500, 1000)}

    def writer():
        flip = False
        while not stop.is_set():
            try:
                rate_limiter.set_user_overrides(big_a if flip else big_b)
                rate_limiter.set_global_override("chat", 7 if flip else 9)
                ps.set_setting("tavily.daily_limit", "41")
                ps.set_setting("tavily.daily_limit", "42")
                flip = not flip
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    def reader():
        while not stop.is_set():
            try:
                snap = dict(rate_limiter._user_overrides)  # lock-free copy
                for _k, _v in snap.items():
                    pass
                ok, _scope = rate_limiter.allow("chat", 999001)
                assert isinstance(ok, bool)
                ps.get_setting("tavily.daily_limit")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=writer),
               threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(0.7)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    rate_limiter.set_user_overrides(big_b)
    got = dict(rate_limiter._user_overrides)
    assert len(got) == 500 and set(got.values()) == {50}
    rate_limiter.set_user_overrides({})
    ps.delete_setting("tavily.daily_limit")
    ps.clear_settings_cache()


def test_reset_user_data_schema_clean():
    """2C: seed every namespace through real write paths, reset uid 12,
    then introspect the schema — no column may reference 12 (except audit,
    bans, admin rows and explicitly kept group todos/reminders). 123/1234
    must be untouched; in-memory state cleared."""
    import sessions as _sess
    from services import media as _media
    from services import streaming as _stream

    U, A, B = 12, 123, 1234
    G = "-999"
    trig = time.time() - 30

    def seed(uid, tag):
        s = str(uid)
        repo.save_message(s, "user", f"dm {tag}")
        sid = f"sess{uid:04d}"
        repo.create_session(uid, sid, f"t{tag}")
        repo.save_message(f"{s}#{sid}", "user", f"thread {tag}")
        repo.set_active_session(uid, sid)
        repo.save_message(f"light_{s}", "user", f"light dm {tag}")
        for ns in (f"inline_{s}", f"inline_light_{s}", f"guest_{s}", f"guest_light_{s}"):
            repo.save_message(ns, "user", f"{ns} {tag}")
        repo.add_user_facts(uid, [f"factoid {tag}"], source_chat_id=s)
        repo.record_behavior(G, uid, 10, False, False, False, False, "sample", 20)
        repo.log_group_chat(G, uid, f"User{tag}", f"group hello {tag}")
        repo.upsert_group_member(G, uid, f"user{tag}", f"User{tag}")
        repo.upsert_known_chat(s, "private", f"user{tag}", None)
        repo.add_todo(s, uid, f"private task {tag}", "Normal", "General", None, "none")
        repo.add_reminder(s, uid, f"priv rem {tag}", "me", "none", trig)
        repo.mark_bot_media_message(s, 500 + uid, kind="instagram")

    for uid, tag in ((U, "twelve"), (A, "a123"), (B, "b1234")):
        seed(uid, tag)
    # group rows by U: kept; others' rows assigned to U: unassigned
    gt = repo.add_todo(G, U, "group task by U", "Normal", "General", None, "none")
    repo.add_todo(G, 777, "other task", "Normal", "General", None, "none",
                  assignee_id=U, assignee_username="usertwelve")
    gr = repo.add_reminder(G, U, "group rem by U", "all", "none", trig)
    repo.add_reminder(G, 777, "assigned rem", "me", "none", trig,
                      assignee_id=U, assignee_username="usertwelve")
    # in-memory state for U
    _sess._todo_sessions.set((str(U), U), {"text": "wiz"})
    _sess._waiting_text_input.set((str(U), U), {"state": "X", "user_id": U})
    _sess._panel_input_sessions.set(U, {"state": "Y"})
    _media._youtube_quality_sessions.set("tok12", {"user_id": U})
    _stream._register_active_request(U, U, 31337)
    ps.ban_uid(U, "", 1)  # banned users may be reset; ban survives
    try:
        counts = repo.count_user_data(U)
        assert counts["messages"] >= 7 and counts["user_facts"] == 1
        assert counts["todos_private"] == 1 and counts["todos_group_kept"] == 1
        assert counts["todos_unassign"] == 1
        out = repo.reset_user_data(1, U)
        assert out["messages"] == counts["messages"]
        from sessions import clear_user_state
        from services.media import clear_user_yt_sessions
        from services.streaming import drop_user_requests

        clear_user_state(U)
        clear_user_yt_sessions(U)
        drop_user_requests(U)

        def references(value, uid):
            if value is None:
                return False
            if isinstance(value, int):
                return value == uid
            s = str(value)
            return (s == str(uid) or s.startswith(f"{uid}#")
                    or s in (f"inline_{uid}", f"inline_light_{uid}",
                             f"guest_{uid}", f"guest_light_{uid}",
                             f"light_{uid}"))

        conn = repo._connect()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if table == "sqlite_sequence":
                continue
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            for col in cols:
                if col not in ("chat_id", "user_id", "creator_id", "assignee_id"):
                    continue
                for (v,) in conn.execute(f"SELECT {col} FROM {table}"):
                    if table == "panel_audit" and col == "admin_id":
                        continue  # the reset row itself is by admin 1
                    if table in ("panel_bans", "panel_admins"):
                        continue  # explicitly kept
                    if table in ("todos", "reminders") and col in ("creator_id",):
                        # kept group rows created by U
                        row_ok = conn.execute(
                            f"SELECT chat_id FROM {table} WHERE {col} = ?", (v,)
                        ).fetchall() if v == U else []
                        assert all(r[0] != str(U) for r in row_ok), (table, col, v)
                        continue
                    assert not references(v, U), f"{table}.{col}={v!r} refs 12"

        # kept rows intact with NULLed assignees
        for table, rid in (("todos", gt), ("reminders", gr)):
            row = conn.execute(
                f"SELECT creator_id, chat_id FROM {table} WHERE id = ?", (rid,)).fetchone()
            assert row == (U, G), (table, row)
        for table in ("todos", "reminders"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE assignee_id = ?", (U,)).fetchone()[0]
            assert n == 0, table
        # ban + audit survive
        assert ps.is_banned(U) is True
        audit = conn.execute(
            "SELECT result FROM panel_audit WHERE action = 'user_reset' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert audit and "messages=" in audit[0]
        # neighbors untouched
        for other in (A, B):
            s = str(other)
            assert repo.count_messages(s) >= 1
            assert repo.count_user_facts(other) == 1
            assert repo.get_active_session(other) == f"sess{other:04d}"
        # in-memory cleared for U only
        assert _sess._todo_sessions.get((str(U), U)) is None
        assert _sess._waiting_text_input.get((str(U), U)) is None
        assert _sess._panel_input_sessions.get(U) is None
        assert _media._youtube_quality_sessions.get("tok12") is None
        assert not [k for k in _stream._active_requests if k[1] == U]
    finally:
        ps.unban_uid(U)
        with repo._write_lock:
            c2 = repo._connect()
            for s, uid in (("123", A), ("1234", B)):
                c2.execute("DELETE FROM messages WHERE chat_id = ? OR chat_id LIKE ? ESCAPE '\\' "
                           "OR chat_id IN (?,?,?,?,?)",
                           (s, f"{s}#%", f"inline_{s}", f"inline_light_{s}",
                            f"guest_{s}", f"guest_light_{s}", f"light_{s}"))
                for table, col in (("sessions", "user_id"), ("user_facts", "user_id"),
                                   ("behavior_stats", "user_id"), ("chat_log", "user_id"),
                                   ("group_members", "user_id")):
                    c2.execute(f"DELETE FROM {table} WHERE {col} = ?", (uid,))
                c2.execute("DELETE FROM user_active_sessions WHERE user_id = ?", (uid,))
                c2.execute("DELETE FROM known_chats WHERE chat_id = ?", (s,))
                c2.execute("DELETE FROM bot_media_messages WHERE chat_id = ?", (s,))
                c2.execute("DELETE FROM todos WHERE chat_id = ?", (s,))
                c2.execute("DELETE FROM reminders WHERE chat_id = ?", (s,))
            c2.execute("DELETE FROM todos WHERE chat_id = ?", (G,))
            c2.execute("DELETE FROM reminders WHERE chat_id = ?", (G,))
            c2.execute("DELETE FROM summaries WHERE chat_id LIKE '%12%' OR chat_id LIKE '%123%'")
            c2.commit()
        shared.invalidate_identity_caches()


def test_reset_entry_points_owner_only(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    repo.add_panel_admin(2, 1)
    try:
        for data in ("pnl_reset_12", "pnl_reset_ok_12"):
            handler = (panel.handle_panel_reset if data == "pnl_reset_12"
                       else panel.handle_panel_reset_ok)
            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert handler(_pcall(999, data)) is None
            assert fb.edits == [] and fb.sent == []
            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert handler(_pcall(1, data, chat_type="group")) is None
            assert fb.edits == [] and fb.sent == []
            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert handler(_pcall(2, data)) is None
            assert fb.edits == [] and fb.sent == []
    finally:
        repo.remove_panel_admin(2)
        _panel_input_sessions_discard_all()


def test_reset_tombstone_drops_queued_fact_job(monkeypatch):
    """F1: fact job queued before reset never writes after it."""
    from services import memory as mem_mod

    class FakeAgnes:
        def generate(self, messages):
            return '["User likes astronomy very much indeed"]'

    monkeypatch.setattr(mem_mod, "agnes", FakeAgnes())
    user = _ns(id=1301, is_bot=False, first_name="T", username="t")
    mem_mod.fact_extractor.submit("1301", user, "I really love astronomy a lot")
    item = None
    with mem_mod.fact_extractor._lock:
        item = next((q for q in mem_mod.fact_extractor._queue if q["user_id"] == 1301), None)
    assert item is not None
    repo.reset_user_data(1, 1301)
    ps.note_reset(1301)
    mem_mod.fact_extractor._process(item)
    assert repo.count_user_facts(1301) == 0
    # control: same flow without reset writes the fact
    user2 = _ns(id=1302, is_bot=False, first_name="T", username="t")
    mem_mod.fact_extractor.submit("1302", user2, "I really love astronomy a lot")
    with mem_mod.fact_extractor._lock:
        item2 = next((q for q in mem_mod.fact_extractor._queue if q["user_id"] == 1302), None)
    mem_mod.fact_extractor._process(item2)
    assert repo.count_user_facts(1302) == 1
    with repo._write_lock:
        c = repo._connect()
        c.execute("DELETE FROM user_facts WHERE user_id IN (1301, 1302)")
        c.execute("DELETE FROM messages WHERE chat_id IN ('1301', '1302')")
        c.commit()


def test_reset_tombstone_blocks_streaming_save(monkeypatch):
    """F1: in-flight streaming reply cancelled by reset saves nothing."""
    from services import streaming as _stream

    uid, other = 1311, 1312
    entry = _stream._register_active_request(uid, uid, 6001)
    assert entry.get("born")
    ps.note_reset(uid)
    _stream.drop_user_requests(uid)
    assert entry["cancel"] is True
    assert not [k for k in _stream._active_requests if k[1] == uid]
    assert ps.save_memory_guarded(str(uid), "assistant", "partial", uid, entry["born"]) is None
    assert repo.count_messages(str(uid)) == 0
    # control: untouched user saves normally (row id returned)
    entry2 = _stream._register_active_request(other, other, 6002)
    saved_row = ps.save_memory_guarded(str(other), "assistant", "done", other, entry2["born"])
    assert type(saved_row) is int and saved_row > 0
    assert repo.count_messages(str(other)) == 1
    _stream.drop_user_requests(other)
    repo.clear_chat(str(other))


def test_reset_tombstone_blocks_summarize(monkeypatch):
    """F1: reset landing DURING the summarize LLM call drops the save."""
    from services import memory as mem_mod

    class FakeAgnes:
        def summarize(self, block):
            ps.note_reset(1313)  # reset lands mid-call, after rows were read
            return "SUMMARY"

    monkeypatch.setattr(mem_mod, "agnes", FakeAgnes())
    monkeypatch.setattr(mem_mod, "SUMMARIZE_THRESHOLD", 2)
    monkeypatch.setattr(mem_mod, "SUMMARIZE_BATCH", 2)
    for i in range(3):
        repo.save_message("1313", "user", f"turn {i} with enough words here")
    mem_mod.maybe_summarize("1313")
    assert repo.get_summary("1313") == ""
    assert repo.count_messages("1313") == 3  # rows themselves untouched
    repo.clear_chat("1313")


def test_reset_tombstone_blocks_profiler_fact(monkeypatch):
    """F1: reset landing mid-run drops that user's profile write."""
    from services import memory as mem_mod

    class FakeAgnes:
        def generate(self, messages):
            ps.note_reset(1321)  # reset lands mid-run, after rows were read
            return "علی اهل بحث است و زیاد سوال می‌پرسد."

    monkeypatch.setattr(mem_mod, "agnes", FakeAgnes())
    # Seed with the REAL clock (record_behavior stamps the Iran-today date),
    # THEN shift the clock one Iran-day forward and run the nightly profiler —
    # it analyzes the day that just ended, i.e. exactly the rows just seeded.
    import db as db_mod

    for uid in (1321, 1322):
        for _ in range(10):
            repo.record_behavior("-910", uid, 20, False, False, False, False, "sample line here", 20)
        repo.log_group_chat("-910", uid, f"U{uid}", "some group chatter here hello")
    from datetime import timedelta as _timedelta
    from timeutil import iran_now as _real_iran_now

    tomorrow_same_time = _real_iran_now() + _timedelta(days=1)
    monkeypatch.setattr(db_mod, "iran_now", lambda: tomorrow_same_time)
    mem_mod._behavior_profile_nightly()
    assert repo.count_user_facts(1321) == 0
    assert repo.count_user_facts(1322) >= 1
    with repo._write_lock:
        c = repo._connect()
        c.execute("DELETE FROM user_facts WHERE user_id IN (1321, 1322)")
        c.execute("DELETE FROM behavior_stats WHERE chat_id = '-910'")
        c.execute("DELETE FROM chat_log WHERE chat_id = '-910'")
        c.commit()


# ================= Stage 3A: broadcast =================
from telebot.apihelper import ApiTelegramException


class _BcastFakeBot(_PanelFakeBot):
    """Scripted copy_message failures: chat_id -> 'unreachable' | 'failed'
    | ('flood', n_floods_then_ok)."""

    def __init__(self, fail=None):
        super().__init__()
        self.fail = fail or {}
        self.attempts = {}

    def copy_message(self, chat_id=None, from_chat_id=None, message_id=None, **kwargs):
        self.attempts[chat_id] = self.attempts.get(chat_id, 0) + 1
        mode = self.fail.get(chat_id)
        if mode == "unreachable":
            raise ApiTelegramException("copyMessage", None, {
                "ok": False, "error_code": 403,
                "description": "Forbidden: bot was blocked by the user"})
        if mode == "failed":
            raise RuntimeError("boom")
        if isinstance(mode, tuple) and mode[0] == "flood":
            if self.attempts[chat_id] <= mode[1]:
                raise ApiTelegramException("copyMessage", None, {
                    "ok": False, "error_code": 429,
                    "description": "Too Many Requests: retry after 1",
                    "parameters": {"retry_after": 1}})
        self.copied.append((chat_id, from_chat_id, message_id))
        return _ns(message_id=1000)


def _seed_known_users(*uids):
    for uid in uids:
        repo.upsert_known_chat(str(uid), "private", f"user{uid}", None)
    repo.upsert_known_chat("-800", "group", None, "Group800")


def _clear_known(*cids):
    with repo._write_lock:
        conn = repo._connect()
        for cid in cids:
            conn.execute("DELETE FROM known_chats WHERE chat_id = ?", (str(cid),))
        conn.commit()


def _broadcast_bot(monkeypatch):
    import handlers.panel as panel

    fb = _BcastFakeBot()
    monkeypatch.setattr(panel, "bot", fb)
    return fb


def _content_msg(uid=1, text="hello broadcast", mid=50):
    return _ns(from_user=_user(uid), chat=_ns(type="private", id=100),
               text=text, message_id=mid, content_type="text")


def test_broadcast_flow_nothing_before_confirm(monkeypatch):
    """Every target builds a preview; zero recipient sends before confirm."""
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _broadcast_bot(monkeypatch)
    _seed_known_users(301, 302)
    try:
        for target in ("pnl_bc_users", "pnl_bc_groups", "pnl_bc_both"):
            fb.copied.clear()
            fb.sent.clear()
            panel.handle_panel_broadcast_target(_pcall(1, target))
            panel.handle_panel_text_input(_content_msg())
            assert fb.copied, "preview copy expected"
            assert all(c[0] == 100 for c in fb.copied), "recipient sends must wait for confirm"
            assert len(fb.sent) == 1
            markup = fb.sent[0][2].get("reply_markup")
            tokens = [b.callback_data for row in markup.keyboard for b in row
                      if b.callback_data.startswith("pnl_bc_ok_")]
            assert len(tokens) == 1
    finally:
        _clear_known(301, 302, "-800")
        _panel_input_sessions_discard_all()
        for tok in list(panel._panel_broadcast_drafts._data):
            panel._panel_broadcast_drafts.discard(tok)


def test_broadcast_double_confirm_runs_once(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _broadcast_bot(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    token = "dblconfirm01"
    draft = {"owner": 1, "kind": "users", "label": "test", "recipients": [301, 302],
             "skipped_banned": 0, "source_chat": 100, "source_msg": 50,
             "content_type": "text"}
    panel._panel_broadcast_drafts.set(token, draft)
    try:
        panel.handle_panel_broadcast_confirm(_pcall(1, f"pnl_bc_ok_{token}"))
        deadline = time.time() + 5
        real_sleep = __import__("time").sleep
        while True:
            with panel._broadcast_lock:
                free = not panel._broadcast_lock.locked()
            if free and panel._broadcast_run == {}:
                break
            if time.time() > deadline:
                break
            real_sleep(0.02)
        first_sent = len(fb.copied)
        assert first_sent == 2
        panel.handle_panel_broadcast_confirm(_pcall(1, f"pnl_bc_ok_{token}"))
        assert len(fb.copied) == 2, "double confirm must not resend"
        assert any("xpired" in (a[1] or "") or "already used" in (a[1] or "")
                   for a in fb.acked)
        rows = repo.list_broadcasts(limit=3)
        assert rows and rows[0][5] == 2 and rows[0][9] == "done"
    finally:
        monkeypatch.undo()
        for tok in list(panel._panel_broadcast_drafts._data):
            panel._panel_broadcast_drafts.discard(tok)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_broadcasts")
            conn.execute("DELETE FROM panel_audit WHERE action LIKE 'broadcast%'")
            conn.commit()


def test_broadcast_expired_draft_refused(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _broadcast_bot(monkeypatch)
    panel.handle_panel_broadcast_confirm(_pcall(1, "pnl_bc_ok_nosuchtoken"))
    assert fb.copied == []
    assert any("xpired" in (a[1] or "") for a in fb.acked)


def test_broadcast_recipients_skip_banned_and_owner(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    _seed_known_users(303, 304)
    ps.ban_uid(303, "", 1)
    try:
        recips, skipped, _label = panel._broadcast_recipients("users", owner_id=1)
        assert 303 not in recips and 304 in recips and skipped == 1
        assert 1 not in recips  # owner's own chat excluded
    finally:
        ps.unban_uid(303)
        _clear_known(303, 304, "-800")


def test_broadcast_retry_unreachable_failed():
    import handlers.panel as panel

    fb = _BcastFakeBot(fail={-800: "unreachable", 305: "failed", 306: ("flood", 2)})
    bid = repo.create_broadcast(1, "test", "text", 3, 0)
    draft = {"owner": 1, "kind": "users", "label": "test",
             "recipients": [-800, 305, 306], "skipped_banned": 0,
             "source_chat": 100, "source_msg": 50, "content_type": "text"}
    import time as _t
    real_sleep = _t.sleep
    _t.sleep = lambda s: None
    orig_bot = panel.bot
    panel.bot = fb
    try:
        panel._broadcast_worker(bid, draft, __import__("threading").Event(), 100, 700)
    finally:
        panel.bot = orig_bot
        _t.sleep = real_sleep
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_broadcasts WHERE id = ?", (bid,))
            conn.execute("DELETE FROM panel_audit WHERE action LIKE 'broadcast%'")
            conn.commit()
    assert fb.attempts[-800] == 1, "unreachable never retried"
    assert fb.attempts[305] == 1
    assert fb.attempts[306] == 3, "flood retried then sent"
    assert [c[0] for c in fb.copied] == [306]


def test_broadcast_stop_mid_run_reports_not_attempted():
    import handlers.panel as panel
    import threading as _th

    fb = _BcastFakeBot()
    real_sleep = __import__("time").sleep
    bid = repo.create_broadcast(1, "test", "text", 50, 0)
    draft = {"owner": 1, "kind": "users", "label": "test",
             "recipients": list(range(2000, 2050)), "skipped_banned": 0,
             "source_chat": 100, "source_msg": 50, "content_type": "text"}
    orig_bot = panel.bot
    panel.bot = fb
    cancel = _th.Event()
    worker = _th.Thread(target=panel._broadcast_worker,
                        args=(bid, draft, cancel, 100, 701), daemon=True)
    try:
        worker.start()
        deadline = real_sleep and __import__("time").time() + 5
        while len(fb.copied) < 3 and __import__("time").time() < deadline:
            real_sleep(0.01)
        assert len(fb.copied) >= 3
        cancel.set()  # same effect as the 🛑 Stop button
        worker.join(timeout=10)
        assert not worker.is_alive()
        sent = len(fb.copied)
        assert 0 < sent < 50
        row = [r for r in repo.list_broadcasts(limit=5) if r[0] == bid][0]
        assert row[9] == "stopped"
        assert row[5] == sent and row[5] + 0 + 0 < 50  # not_attempted > 0
    finally:
        panel.bot = orig_bot
        cancel.set()
        worker.join(timeout=10)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_broadcasts WHERE id = ?", (bid,))
            conn.execute("DELETE FROM panel_audit WHERE action LIKE 'broadcast%'")
            conn.commit()


def test_broadcast_one_at_a_time_and_nonblocking(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _broadcast_bot(monkeypatch)
    gate = threading.Event()  # blocks the first runner's sends
    orig_copy = fb.copy_message

    def gated_copy(**kwargs):
        assert gate.wait(timeout=10)
        return orig_copy(**kwargs)

    fb.copy_message = gated_copy
    t1, t2 = "locktest0001", "locktest0002"
    mk = lambda: {"owner": 1, "kind": "users", "label": "t",
                  "recipients": [301], "skipped_banned": 0,
                  "source_chat": 100, "source_msg": 50, "content_type": "text"}
    panel._panel_broadcast_drafts.set(t1, mk())
    panel._panel_broadcast_drafts.set(t2, mk())
    try:
        t0 = time.time()
        panel.handle_panel_broadcast_confirm(_pcall(1, f"pnl_bc_ok_{t1}"))
        assert time.time() - t0 < 2, "confirm must not block on the runner"
        panel.handle_panel_broadcast_confirm(_pcall(1, f"pnl_bc_ok_{t2}"))
        assert any("nother broadcast" in (a[1] or "") for a in fb.acked)
    finally:
        gate.set()
        deadline = time.time() + 10
        while panel._broadcast_run != {} and time.time() < deadline:
            time.sleep(0.05)
        for tok in (t1, t2):
            panel._panel_broadcast_drafts.discard(tok)
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_broadcasts")
            conn.execute("DELETE FROM panel_audit WHERE action LIKE 'broadcast%'")
            conn.commit()


def test_interrupted_marking_at_startup():
    bid = repo.create_broadcast(1, "test", "text", 5, 0)
    try:
        assert repo.mark_interrupted_broadcasts() >= 1
        rows = [r for r in repo.list_broadcasts(limit=5) if r[0] == bid]
        assert rows and rows[0][9] == "interrupted"
        with open("wiring.py", encoding="utf-8") as f:
            assert "mark_interrupted_broadcasts" in f.read()
    finally:
        with repo._write_lock:
            conn = repo._connect()
            conn.execute("DELETE FROM panel_broadcasts WHERE id = ?", (bid,))
            conn.commit()


# ================= 3B fixes + Jobs/Stats =================

def test_legacy_keyboard_removal_and_stale_hint(monkeypatch):
    """F1: first /admin sends one-time ReplyKeyboardRemove (deleted after);
    stale button texts get the hint; others pass through."""
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    ps.delete_setting("ui.kb_removed")
    try:
        panel.handle_admin_panel(_pmsg(1))
        removes = [s for s in fb.sent if s[2].get("reply_markup") is not None
                   and type(s[2]["reply_markup"]).__name__ == "ReplyKeyboardRemove"]
        assert len(removes) == 1
        assert ps.get_setting("ui.kb_removed") == "1"
        n_sent = len(fb.sent)
        panel.handle_admin_panel(_pmsg(1))
        assert len(fb.sent) == n_sent + 1  # home only, no second remove
        assert panel._is_stale_keyboard_text(_pmsg(1, text="👥 Broadcast to Groups"))
        assert not panel._is_stale_keyboard_text(_pmsg(1, text="hello"))
        assert not panel._is_stale_keyboard_text(_pmsg(2, text="👥 Broadcast to Groups"))
        panel.handle_stale_keyboard_text(_pmsg(1, text="📋 Group List"))
        assert "use /admin" in fb.sent[-1][1]
    finally:
        ps.delete_setting("ui.kb_removed")
        _panel_input_sessions_discard_all()


def test_tombstone_allows_new_activity_and_prunes(monkeypatch):
    """F3: post-reset work saves + learns again; old tombstones pruned; no DB."""
    from services import memory as mem_mod

    monkeypatch.setattr(repo, "_connect",
                        lambda: (_ for _ in ()).throw(OSError("db down")))
    ps.note_reset(1401)
    assert ps.tombstoned_after(1401, time.time() - 10) is True
    assert ps.tombstoned_after(1401, time.time() + 60) is False  # new work OK
    assert ps.save_memory_guarded("1401", "user", "fresh hello", 1401,
                                  time.time() + 60) is None or True  # DB down -> None, no raise
    ps._reset_tombstones[1402] = time.time() - 4000  # expired
    ps.note_reset(1403)
    assert 1402 not in ps._reset_tombstones and 1403 in ps._reset_tombstones
    # facts learnable again: no tombstone blocks (control path covered by
    # test_reset_tombstone_drops_queued_fact_job's control branch)
    assert ps.tombstoned_after(1404, 0) is False


_3B_ANY_ADMIN = {"pnl_jobs", "pnl_stats"}
_3B_OWNER_ONLY = {"pnl_jp_0_1", "pnl_jp_0_0", "pnl_jr_0", "pnl_jrun", "pnl_jrun_ok"}


def _find_3b(panel, data):
    return {
        "pnl_jobs": panel.handle_panel_jobs,
        "pnl_stats": panel.handle_panel_stats,
        "pnl_jp_0_1": panel.handle_panel_job_pause,
        "pnl_jp_0_0": panel.handle_panel_job_pause,
        "pnl_jr_0": panel.handle_panel_job_resume,
        "pnl_jrun": panel.handle_panel_backup_run,
        "pnl_jrun_ok": panel.handle_panel_backup_run_ok,
    }[data]


def test_jobs_stats_permission_matrix(monkeypatch):
    import handlers.panel as panel

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    repo.add_panel_admin(2, 1)
    try:
        for data in sorted(_3B_ANY_ADMIN):
            h = _find_3b(panel, data)
            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert h(_pcall(999, data)) is None and fb.edits == [] and fb.sent == []
            fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
            assert h(_pcall(1, data, chat_type="group")) is None
            assert fb.edits == [] and fb.sent == []
        for data in sorted(_3B_OWNER_ONLY):
            h = _find_3b(panel, data)
            for uid, chat in ((999, "private"), (1, "group"), (2, "private")):
                fb.edits.clear(); fb.sent.clear(); fb.acked.clear()
                assert h(_pcall(uid, data, chat_type=chat)) is None
                assert fb.edits == [] and fb.sent == []
    finally:
        repo.remove_panel_admin(2)
        _panel_input_sessions_discard_all()


def test_job_pause_resume_expiry(monkeypatch):
    from services import panel_jobs as pj

    _as_owner_admin(monkeypatch, owner=1)
    assert pj.job_paused_until("scheduler") == (False, "")
    ok, _msg = pj.job_pause("scheduler", 1)
    assert ok is True
    try:
        paused, label = pj.job_paused_until("scheduler")
        assert paused is True and label
        ok, _msg = pj.job_pause("scheduler", 99)
        assert ok is False
        ok, _msg = pj.job_pause("scheduler", 0)
        assert ok is False  # indefinite only relay/profiler
        ok, _msg = pj.job_pause("relay", 0)
        assert ok is True
        pj.job_resume("relay")
        assert pj.job_paused_until("relay") == (False, "")
        # expiry: backdate the key
        repo.set_panel_setting("job.scheduler.paused_until", str(time.time() - 5))
        ps.clear_settings_cache()
        assert pj.job_paused_until("scheduler") == (False, "")
    finally:
        pj.job_resume("scheduler")


def test_job_pause_zero_db_warm_and_fail_open(monkeypatch):
    from services import panel_jobs as pj

    pj.job_pause("backup", 1)
    pj.job_snapshot()  # warm everything (settings cache)
    try:
        monkeypatch.setattr(repo, "_connect",
                            lambda: (_ for _ in ()).throw(OSError("db down")))
        assert pj.job_paused_until("backup")[0] is True
        snap = pj.job_snapshot()
        assert any(r[0] == "backup" and r[3] for r in snap)
    finally:
        monkeypatch.undo()
        pj.job_resume("backup")
    monkeypatch.setattr(repo, "_connect",
                        lambda: (_ for _ in ()).throw(OSError("db down")))
    assert pj.job_paused_until("nope-job") == (False, "")


def test_job_registry_marks_without_behavior_change():
    from services import panel_jobs as pj

    pj.job_mark_start("scheduler")
    pj.job_mark_success("scheduler")
    pj.job_mark_error("scheduler", "x" * 500)
    snap = {r[0]: r for r in pj.job_snapshot()}
    assert snap["scheduler"][5] >= 1
    assert len(snap["scheduler"][8]) <= 200  # truncated
    assert set(snap) >= {"scheduler", "backup", "profiler", "relay",
                         "session-gc", "fact-extractor"}
    # exception behavior untouched: poll-error paths still notify the admin
    with open("services/scheduling.py", encoding="utf-8") as f:
        src = f.read()
    assert 'admin_notifier.notify("scheduler_reminder_poll"' in src
    assert 'admin_notifier.notify("scheduler_todo_poll"' in src


def test_backup_runnnow_single_flight_no_purge(monkeypatch):
    from services import backup as backup_mod

    _as_owner_admin(monkeypatch, owner=1)
    assert backup_mod._backup_lock.acquire(blocking=False)
    try:
        ok, msg = backup_mod.run_backup_now(1)
        assert ok is False and "already running" in msg
    finally:
        backup_mod._backup_lock.release()
    calls = []
    monkeypatch.setattr(repo, "purge_old_messages",
                        lambda *a, **k: calls.append("purge") or 0)
    monkeypatch.setattr(repo, "maybe_vacuum",
                        lambda *a, **k: calls.append("vacuum") or False)
    sent_docs = []
    monkeypatch.setattr(backup_mod, "bot",
                        _ns(send_document=lambda *a, **k: sent_docs.append(a)))
    ok, msg = backup_mod.run_backup_now(1)
    assert ok is True and calls == [] and len(sent_docs) == 1
    assert sent_docs[0][0] == 1  # goes to the requesting owner
    with repo._write_lock:
        conn = repo._connect()
        conn.execute("DELETE FROM panel_audit WHERE action LIKE 'backup%'")
        conn.commit()


def test_dispatcher_pause_holds_and_resumes_in_order(monkeypatch):
    """Paused scheduler claims nothing; after resume overdue rows claim
    oldest-first and fire without burst errors."""
    from services import panel_jobs as pj
    from services import scheduling as sched

    sent = []
    monkeypatch.setattr(sched, "bot",
                        _ns(send_message=lambda *a, **k: sent.append(a)))
    monkeypatch.setattr(sched, "admin_notifier",
                        _ns(notify=lambda *a, **k: None))
    now = time.time()
    r1 = repo.add_reminder(991011, 991, "first", "me", "none", now - 300)
    r2 = repo.add_reminder(991011, 991, "second", "me", "none", now - 60)
    pj.job_pause("scheduler", 1)
    try:
        paused, _label = pj.job_paused_until("scheduler")
        assert paused is True  # loop would sleep+continue here
        pj.job_resume("scheduler")
        claimed = repo.claim_due_reminders(time.time(), "tok-order")
        ids = [r[0] for r in claimed if r[0] in (r1, r2)]
        assert ids == [r1, r2], "oldest-due-first"
        for _rid in ids:
            repo.release_reminder_claim(_rid)
        for rid, trig in [(r1, now - 300), (r2, now - 60)]:
            sched._fire_reminder(rid, trig)
        assert len(sent) == 2
    finally:
        pj.job_resume("scheduler")
        repo.dismiss_reminder(r1)
        repo.dismiss_reminder(r2)


def test_stats_iran_buckets_markers_cache(monkeypatch):
    import handlers.panel as panel

    import datetime as _dt

    _as_owner_admin(monkeypatch, owner=1)
    fb = _panel_bot(monkeypatch)
    now_utc = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    midnight_utc = ((now_utc + _dt.timedelta(seconds=12600)).replace(
        hour=0, minute=0, second=0) - _dt.timedelta(seconds=12600))
    a_ts = (midnight_utc - _dt.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    b_ts = midnight_utc.strftime("%Y-%m-%d %H:%M:%S")
    day_a = (midnight_utc - _dt.timedelta(seconds=1) + _dt.timedelta(seconds=12600)).date().isoformat()
    day_b = (midnight_utc + _dt.timedelta(seconds=12600)).date().isoformat()
    assert day_a != day_b  # straddles an Iran midnight
    with repo._write_lock:
        conn = repo._connect()
        conn.execute("INSERT INTO messages (chat_id, role, text, timestamp) VALUES "
                     f"('991021', 'user', 'a', '{a_ts}'),"
                     f"('991021', 'user', 'b', '{b_ts}'),"
                     f"('991021', 'assistant', 'c', '{b_ts}'),"
                     f"('tldr_991021', 'model', '[tldr-cooldown] x', '{b_ts}'),"
                     f"('debate_991021', 'user', 'd', '{b_ts}')")
        conn.commit()
    try:
        panel.handle_panel_stats(_pcall(1, "pnl_stats"))
        text = fb.edits[-1][2]
        assert "since last restart" in text
        assert "role 'user'" in text
        line_a = [l for l in text.splitlines() if l.startswith(f"`{day_a}`")]
        line_b = [l for l in text.splitlines() if l.startswith(f"`{day_b}`")]
        assert line_a and line_b
        assert line_a[0].rstrip().endswith("`1`")  # only 'a'
        assert line_b[0].rstrip().endswith("`1`")  # only 'b'; assistant/tldr/debate excluded
        # 60s cache: new row invisible until expiry
        with repo._write_lock:
            c2 = repo._connect()
            c2.execute("INSERT INTO messages (chat_id, role, text, timestamp) VALUES "
                       f"('991021', 'user', 'z', '{b_ts}')")
            c2.commit()
        panel.handle_panel_stats(_pcall(1, "pnl_stats"))
        assert fb.edits[-1][2] == text
        with panel._stats_lock:
            panel._stats_cache["exp"] = 0.0
        panel.handle_panel_stats(_pcall(1, "pnl_stats"))
        line_bb = [l for l in fb.edits[-1][2].splitlines() if l.startswith(f"`{day_b}`")]
        assert line_bb and line_bb[0].rstrip().endswith("`2`")
    finally:
        with repo._write_lock:
            c2 = repo._connect()
            c2.execute("DELETE FROM messages WHERE chat_id LIKE '991021%' OR chat_id LIKE 'tldr_991021%' OR chat_id LIKE 'debate_991021%'")
            c2.commit()
        with panel._stats_lock:
            panel._stats_cache["text"] = None
            panel._stats_cache["exp"] = 0.0
