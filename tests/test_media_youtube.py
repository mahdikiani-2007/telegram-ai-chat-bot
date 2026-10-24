"""Tests for the YouTube anti-gate work: URL coverage, android player-
client fallback + remembrance, failure-reason plumbing, and download
attempt ordering. Network fully stubbed (yt_dlp.YoutubeDL faked)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt_dlp

from services import media as yt


@pytest.fixture
def clean_yt_state(monkeypatch):
    saved_mode = dict(yt._yt_last_good_mode)
    monkeypatch.setitem(yt._yt_last_good_mode, "use_cookies", None)
    monkeypatch.setitem(yt._yt_last_good_mode, "player_client", "default")
    yield
    yt._yt_last_good_mode.update(saved_mode)


GATE_MSG = "ERROR: [youtube] x: Sign in to confirm you're not a bot. Bug."
DEAD_MSG = "ERROR: [youtube] x: Video unavailable. Bug."

FAKE_INFO = {
    "title": "  Probe Video  ",
    "formats": [
        {"url": "http://x/v360", "vcodec": "avc1", "acodec": "mp4a",
         "height": 360, "tbr": 500, "filesize": 1000},
        {"url": "http://x/v1080", "vcodec": "avc1", "acodec": "none",
         "height": 1080, "tbr": 4000, "filesize": 8000},
        {"url": "http://x/a", "vcodec": "none", "acodec": "opus",
         "abr": 128, "filesize": 500},
    ],
}


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
    "https://youtu.be/aqz-KE-bpKQ?si=abc",
    "https://www.youtube.com/shorts/abc123XYZ_-",
    "https://m.youtube.com/watch?v=aqz-KE-bpKQ",
    "https://music.youtube.com/watch?v=aqz-KE-bpKQ",
    "https://www.youtube.com/live/jfKfPfyJRdk",
    "https://www.youtube.com/embed/aqz-KE-bpKQ",
    "https://www.youtube-nocookie.com/embed/aqz-KE-bpKQ",
    "https://www.youtube.com/clip/UgkxABC123",
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=42s",
])
def test_url_coverage(url):
    assert yt.extract_youtube_urls(url) == [url]


@pytest.mark.parametrize("url", [
    "https://vimeo.com/123456",
    "https://example.com/watch?v=aqz-KE-bpKQ",
    "just some text",
    "",
])
def test_url_rejections(url):
    assert yt.extract_youtube_urls(url) == []


def test_base_opts_default_has_no_client_or_cookies():
    opts = yt._youtube_base_ydl_opts(use_cookies=False)
    assert "extractor_args" not in opts
    assert "cookiefile" not in opts
    assert opts["js_runtimes"] == {"deno": {}}


def test_base_opts_android_client():
    opts = yt._youtube_base_ydl_opts(use_cookies=False, player_client="android")
    assert opts["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def _scripted_ydl(monkeypatch, script, seen):
    """Fake YoutubeDL: script maps attempt index -> ('raise', msg) | ('ok', info)."""
    calls = {"n": 0}

    class FakeDL:
        def __init__(self, opts):
            seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            kind, payload = script[min(calls["n"], len(script) - 1)]
            calls["n"] += 1
            if kind == "raise":
                raise yt_dlp.utils.DownloadError(payload)
            return payload

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDL)
    return calls


def test_extraction_android_fallback_and_remembrance(clean_yt_state, monkeypatch, tmp_path):
    seen = []
    _scripted_ydl(monkeypatch, [
        ("raise", GATE_MSG),   # anonymous+web gated
        ("ok", FAKE_INFO),     # anonymous+android works
    ], seen)
    title, options, reason = yt._extract_youtube_qualities("https://youtu.be/x")
    assert (title, reason) == ("Probe Video", None)
    assert "v360" in options and "audio" in options
    assert yt._yt_last_good_mode == {"use_cookies": False, "player_client": "android"}
    assert len(seen) == 2
    assert "extractor_args" not in seen[0]
    assert seen[1]["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def test_extraction_unavailable_reason(clean_yt_state, monkeypatch):
    seen = []
    _scripted_ydl(monkeypatch, [("raise", DEAD_MSG)], seen)
    assert yt._extract_youtube_qualities("https://youtu.be/x") == (None, None, "unavailable")
    assert len(seen) == 1  # non-gate: no fallback attempts


def test_extraction_gate_reason_when_all_fail(clean_yt_state, monkeypatch):
    seen = []
    _scripted_ydl(monkeypatch, [("raise", GATE_MSG)] * 5, seen)
    assert yt._extract_youtube_qualities("https://youtu.be/x") == (None, None, "gate")
    assert any("extractor_args" in o for o in seen)  # android was tried


def test_download_prefers_remembered_combo(clean_yt_state, monkeypatch, tmp_path):
    yt._yt_last_good_mode.update({"use_cookies": False, "player_client": "android"})
    dest = tmp_path / "dl"
    dest.mkdir()
    seen = []

    class FakeDL:
        def __init__(self, opts):
            seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            fp = os.path.join(str(dest), "file.mp3")
            with open(fp, "wb") as f:
                f.write(b"0" * 16)
            return {"requested_downloads": [{"filepath": fp}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDL)
    opt = {"is_audio": True, "height": None, "label": "audio", "filesize": 16}
    got = yt._download_youtube_quality("https://youtu.be/x", opt, str(dest))
    assert got and os.path.isfile(got)
    first = seen[0]
    assert first["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    assert "cookiefile" not in first
