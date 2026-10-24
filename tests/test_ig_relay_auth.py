import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ig_relay


class FakeBot:
    def __init__(self):
        self.replies = []

    def reply_to(self, message, text):
        self.replies.append(text)


class FakeClient:
    def __init__(self):
        self.logout_calls = 0

    def logout(self):
        self.logout_calls += 1


def make_relay(tmp_path):
    relay = ig_relay.InstagramRelay(
        {
            "bot": FakeBot(),
            "db_file": str(tmp_path / "relay.db"),
            "download_post": lambda url: None,
        }
    )
    relay.session_file = str(tmp_path / "session.json")
    return relay


def test_logout_invalidates_client_and_saved_session(tmp_path):
    relay = make_relay(tmp_path)
    client = FakeClient()
    relay._client = client
    relay.state["logged_in"] = True
    with open(relay.session_file, "w", encoding="utf-8") as handle:
        handle.write("{}")

    relay.logout()

    assert client.logout_calls == 1
    assert relay._client is None
    assert relay.state["logged_in"] is False
    assert not os.path.exists(relay.session_file)


def test_login_command_starts_login_attempt(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    called = []
    monkeypatch.setattr(relay, "try_login", lambda: called.append(True) or True)
    message = SimpleNamespace(text="/relay login", reply_to_message=None)

    relay.handle_command(message)

    assert called == [True]
    assert relay.bot.replies == ["✅ لاگین با موفقیت انجام شد."]


def test_logout_command_reports_success(tmp_path):
    relay = make_relay(tmp_path)
    relay._client = FakeClient()
    message = SimpleNamespace(text="/relay logout", reply_to_message=None)

    relay.handle_command(message)

    assert relay.bot.replies == ["✅ از اینستاگرام خارج شد و session ذخیره‌شده حذف شد."]
