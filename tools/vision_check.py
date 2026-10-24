"""Live diagnostic for the vision pipeline (dev tool — never imported by the bot).

Reproduces exactly what a photo turn sends: a small PNG + a question, through
the VISION_* config (defaulting to the Agnes hub). Prints per-step results so
a "I can't see images" report can be attributed to a layer:

  1. VISION endpoint reachable?        (GET /models)
  2. Vision model answers at all?      (text-only chat completion)
  3. Vision model SEES the image?      (multimodal completion — expects "red")

Usage:  python3 tools/vision_check.py
Env:    reads the project .env (if present) into the environment first;
        VISION_MODEL / VISION_API_BASE / VISION_API_KEY override, then
        AGNES_BASE / AGNES_API_KEY.
"""
import base64
import json
import os
import struct
import sys
import zlib

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env():
    for path in (os.path.join(ROOT, ".env"), os.path.join(ROOT, "refactored", ".env")):
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


def _red_png():
    width = height = 64
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(typ, data):
        blob = struct.pack(">I", len(data)) + typ + data
        return blob + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main():
    _load_env()
    from config import VISION_API_BASE, VISION_API_KEY, VISION_MODEL

    base = VISION_API_BASE.rstrip("/")
    headers = {"Authorization": f"Bearer {VISION_API_KEY}", "Content-Type": "application/json"}
    print(f"vision endpoint: {base}")
    print(f"vision model:    {VISION_MODEL}\n")

    print("== 1. GET /models ==")
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=20)
        print(f"   status {r.status_code} — endpoint reachable")
    except Exception as exc:
        print(f"   FAILED: {type(exc).__name__}: {str(exc)[:150]}")
        return 1

    print("== 2. text-only completion ==")
    try:
        r = requests.post(f"{base}/chat/completions", headers=headers, json={
            "model": VISION_MODEL, "stream": False,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        }, timeout=60)
        if r.status_code == 200:
            print(f"   status 200 — content: {(r.json()['choices'][0]['message'].get('content') or '').strip()[:80]!r}")
        else:
            print(f"   status {r.status_code}: {json.dumps(r.json())[:200]}")
            return 1
    except Exception as exc:
        print(f"   FAILED: {type(exc).__name__}: {str(exc)[:150]}")
        return 1

    print("== 3. multimodal completion (64x64 red PNG) ==")
    b64 = base64.b64encode(_red_png()).decode()
    try:
        r = requests.post(f"{base}/chat/completions", headers=headers, json={
            "model": VISION_MODEL, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": "What is the dominant color of this image? Answer with one word."},
            ]}],
        }, timeout=90)
        if r.status_code == 200:
            answer = (r.json()["choices"][0]["message"].get("content") or "").strip()
            print(f"   status 200 — answer: {answer[:80]!r}")
            if "red" in answer.lower():
                print("\nVISION OK — the model sees images. Photo turns will work.")
                return 0
            print("\nMODEL REPLIED BUT DID NOT DESCRIBE THE COLOR — "
                  "the endpoint probably strips image parts. Pick another VISION_MODEL.")
            return 1
        print(f"   status {r.status_code}: {json.dumps(r.json())[:300]}")
        return 1
    except Exception as exc:
        print(f"   FAILED: {type(exc).__name__}: {str(exc)[:150]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
