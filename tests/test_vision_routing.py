"""Vision routing: photo turns must go to a dedicated vision endpoint, not
the chat router (the deployed chat router answers 'I can't see' for images
even though the underlying providers have vision — verified live 2026-10-23:
agnes-3.0-flash/2.5-flash correctly describe a test image).

Hermetic: no network; agnes/bot/HTTP monkeypatched per test.
"""
import importlib
import os
import sys
import types as std_types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
from infra import _vision_headers
from ai import agnes, agnes_vision


def _ns(**kwargs):
    obj = std_types.SimpleNamespace()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _user(uid):
    return _ns(id=uid, is_bot=False, first_name="T")


# =========================
# Config: dedicated vision endpoint, defaulting to the Agnes hub
# =========================
def test_vision_config_defaults_to_agnes_hub():
    importlib.reload(config_mod)
    assert config_mod.VISION_API_BASE == config_mod.AGNES_BASE
    assert config_mod.VISION_API_KEY == config_mod.AGNES_API_KEY
    assert config_mod.VISION_MODEL == "agnes-3.0-flash"


def test_vision_config_env_overrides(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "my-vision-model")
    monkeypatch.setenv("VISION_API_BASE", "https://vision.example/v1")
    monkeypatch.setenv("VISION_API_KEY", "vk-test")
    importlib.reload(config_mod)
    try:
        assert config_mod.VISION_MODEL == "my-vision-model"
        assert config_mod.VISION_API_BASE == "https://vision.example/v1"
        assert config_mod.VISION_API_KEY == "vk-test"
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)


def test_vision_headers_use_vision_key():
    hdr = _vision_headers()
    assert hdr["Authorization"] == f"Bearer {config_mod.VISION_API_KEY}"


def test_agnes_vision_client_targets_vision_endpoint_and_model():
    # type() guards against bool-is-int style accidents: chat_base is a str
    assert agnes_vision.chat_base == config_mod.VISION_API_BASE
    assert agnes_vision.chat_model == config_mod.VISION_MODEL
    assert agnes_vision.chat_headers_fn is _vision_headers
    # the plain chat client must stay on the chat router
    assert agnes.chat_base == config_mod.CHAT_API_BASE
    assert agnes.chat_model == config_mod.CHAT_MODEL


# =========================
# Streaming: optional client override
# =========================
class _FakeVisionClient:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages):
        self.calls.append(messages)
        yield ("delta", "seen")
        yield ("delta", " it")


def test_stream_generic_uses_client_override(monkeypatch):
    import services.streaming as streaming_mod

    class _MustNotCall:
        def stream_chat(self, messages):
            raise AssertionError("module-level agnes must not be used when client is passed")

    monkeypatch.setattr(streaming_mod, "agnes", _MustNotCall())

    edits = []

    class _Bot:
        def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
            edits.append(text)

        def send_message(self, chat_id, text, **kw):
            return _ns(message_id=2)

    msg = _ns(chat=_ns(id=1), message_id=9, from_user=_user(5))
    fake = _FakeVisionClient()
    full, err, msg_ids = streaming_mod.stream_to_telegram_generic(
        _Bot(), msg, [{"role": "user", "content": "q"}], 77, client=fake)
    assert err is None
    assert full == "seen it"
    assert fake.calls, "override client must be the one consulted"


def test_stream_generic_defaults_to_module_agnes(monkeypatch):
    import services.streaming as streaming_mod

    fake = _FakeVisionClient()
    monkeypatch.setattr(streaming_mod, "agnes", fake)

    class _Bot:
        def edit_message_text(self, *a, **k):
            pass

        def send_message(self, chat_id, text, **kw):
            return _ns(message_id=2)

    msg = _ns(chat=_ns(id=1), message_id=9, from_user=_user(6))
    streaming_mod.stream_to_telegram_generic(
        _Bot(), msg, [{"role": "user", "content": "q"}], 77)
    assert fake.calls, "default path must consult module-level agnes"


# =========================
# Handlers: photo turns pick the vision client, text turns don't
# =========================
def _patch_chat_pipeline(monkeypatch, captured):
    import handlers.chat as chat_mod

    class _Bot:
        def send_chat_action(self, *a, **k):
            return True

        def get_file(self, file_id):
            return _ns(file_path="photos/x.jpg")

        def edit_message_text(self, *a, **k):
            pass

        def delete_message(self, *a, **k):
            pass

    monkeypatch.setattr(chat_mod, "bot", _Bot())
    monkeypatch.setattr(chat_mod, "HTTP",
                        _ns(get=lambda url, timeout=None: _ns(status_code=200, content=b"x" * 300)))
    monkeypatch.setattr(chat_mod, "get_bot_identity",
                        lambda: {"username": "lbot", "id": 123})
    monkeypatch.setattr(chat_mod, "_safe_reply_to",
                        lambda bot_instance, message, text, **kw: _ns(message_id=77))
    monkeypatch.setattr(chat_mod.ps, "feature_gate_message", lambda *a, **k: True)
    monkeypatch.setattr(chat_mod.ps, "save_memory_guarded", lambda *a, **k: 1)
    monkeypatch.setattr(chat_mod, "maybe_web_search_with_status", lambda *a, **k: "")
    monkeypatch.setattr(chat_mod, "build_context", lambda memory_id: [])
    monkeypatch.setattr(chat_mod, "_inject_user_profile", lambda *a, **k: None)
    monkeypatch.setattr(chat_mod, "maybe_summarize", lambda *a, **k: None)
    monkeypatch.setattr(chat_mod, "finalize_message", lambda *a, **k: None)
    monkeypatch.setattr(chat_mod.repo, "is_bot_media_message", lambda *a, **k: False)
    monkeypatch.setattr(chat_mod.fact_extractor, "submit", lambda *a, **k: None)

    def _recorder(message, messages, placeholder_id, request_entry=None, client=None):
        captured["client"] = client
        return ("final text", None, [(1, 2)])

    monkeypatch.setattr(chat_mod, "stream_to_telegram", _recorder)
    return chat_mod


def test_photo_turn_streams_via_vision_client(monkeypatch):
    captured = {}
    chat_mod = _patch_chat_pipeline(monkeypatch, captured)
    msg = _ns(from_user=_user(771), chat=_ns(type="private", id=771),
              content_type="photo", text=None, caption=None,
              photo=[_ns(file_id="fid")], reply_to_message=None, message_id=3)
    chat_mod.handle_all_messages(msg)
    assert captured.get("client") is chat_mod.agnes_vision, \
        "photo turns must stream through the vision client"


def test_text_turn_keeps_chat_client(monkeypatch):
    captured = {}
    chat_mod = _patch_chat_pipeline(monkeypatch, captured)
    msg = _ns(from_user=_user(772), chat=_ns(type="private", id=772),
              content_type="text", text="سلام", reply_to_message=None, message_id=4)
    chat_mod.handle_all_messages(msg)
    assert captured.get("client") is None, "text turns must keep the chat router"


def test_translate_photo_streams_via_vision_client(monkeypatch):
    import handlers.surfaces as surfaces_mod

    captured = {}

    class _Bot:
        def send_chat_action(self, *a, **k):
            return True

        def get_file(self, file_id):
            return _ns(file_path="photos/y.jpg")

        def edit_message_text(self, *a, **k):
            pass

        def delete_message(self, *a, **k):
            pass

    monkeypatch.setattr(surfaces_mod, "bot", _Bot())
    monkeypatch.setattr(surfaces_mod, "HTTP",
                        _ns(get=lambda url, timeout=None: _ns(status_code=200, content=b"x" * 300)))
    monkeypatch.setattr(surfaces_mod, "_safe_reply_to",
                        lambda bot_instance, message, text, **kw: _ns(message_id=78))
    monkeypatch.setattr(surfaces_mod, "rate_limited_reply", lambda *a, **k: True)
    monkeypatch.setattr(surfaces_mod.ps, "feature_gate_message", lambda *a, **k: True)
    monkeypatch.setattr(surfaces_mod, "finalize_message", lambda *a, **k: None)

    def _recorder(message, messages, placeholder_id, request_entry=None, client=None):
        captured["client"] = client
        return ("ترجمه", None, [(1, 2)])

    monkeypatch.setattr(surfaces_mod, "stream_to_telegram", _recorder)

    msg = _ns(from_user=_user(773), chat=_ns(type="private", id=773),
              content_type="photo", text=None, caption="/tr en what is this",
              photo=[_ns(file_id="fid")], reply_to_message=None, message_id=5)
    surfaces_mod.handle_translate(msg)
    assert captured.get("client") is surfaces_mod.agnes_vision, \
        "photo translations must stream through the vision client"
