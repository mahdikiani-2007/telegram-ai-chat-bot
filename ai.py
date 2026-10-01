import json
import logging
import time

import requests
import urllib3

from config import AGNES_BASE, CHAT_API_BASE, CHAT_MODEL, CHAT_RETRY_ATTEMPTS, IMAGE_MODEL
from infra import HTTP, _chat_headers, _image_headers

log = logging.getLogger("L-bot")


# =========================
# Agnes AI Client (chat-stream + summary + image)
# =========================
class AgnesClient:
    def __init__(
        self, session, chat_base, image_base, chat_headers_fn, image_headers_fn
    ):
        self.session = session
        self.chat_base = chat_base
        self.image_base = image_base
        self.chat_headers_fn = chat_headers_fn
        self.image_headers_fn = image_headers_fn

    def _chat_completion_with_retry(self, payload, timeout):
        """Shared retry helper for non-streaming /chat/completions calls
        (used by summarize() and generate()). CHAT_API_BASE load-balances
        across multiple backend providers; a non-200 from one attempt
        usually just means that particular backend was flaky, so we retry
        the same request a couple of times (CHAT_RETRY_ATTEMPTS) before
        giving up — a fresh attempt often lands on a different, working
        provider. Returns the reply text, or '' if every attempt failed."""
        attempts = CHAT_RETRY_ATTEMPTS + 1
        for attempt in range(1, attempts + 1):
            try:
                r = self.session.post(
                    f"{self.chat_base}/chat/completions",
                    json=payload,
                    headers=self.chat_headers_fn(),
                    timeout=timeout,
                )
                if r.status_code == 200:
                    r.encoding = "utf-8"
                    return r.json()["choices"][0]["message"]["content"] or ""
                log.warning(
                    "chat/completions non-200 (attempt %d/%d): %d — %s",
                    attempt, attempts, r.status_code, r.text[:500],
                )
            except Exception:
                log.warning("chat/completions request error (attempt %d/%d)", attempt, attempts, exc_info=True)
            if attempt < attempts:
                time.sleep(0.6 * attempt)
        return ""

    def summarize(self, text_block):
        """One-shot non-streaming summary call. Returns text or '' on failure."""
        prompt = (
            "Summarize the following conversation excerpt into a concise, "
            "third-person 'case file' note that preserves key facts, names, "
            "decisions and unresolved threads. Keep it under 200 words.\n\n"
            + text_block
        )
        return self._chat_completion_with_retry(
            {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=120,
        )

    def stream_chat(self, messages):
        """Yield incremental content chunks (str) from the streaming endpoint.

        Yields ("delta", text) for content pieces, ("status", code) if a
        non-200 occurs before any content (after retries are exhausted), or
        ("error", reason) if the connection drops / times out mid-stream
        (after some content may already have been yielded). Callers MUST
        handle "error" instead of letting the underlying network exception
        propagate, otherwise a transient hiccup mid-stream kills the whole
        request and leaves any already-sent Telegram message stuck
        half-written.

        Retries (CHAT_RETRY_ATTEMPTS) only apply BEFORE any content has been
        yielded — same reasoning as _chat_completion_with_retry: the chat
        endpoint load-balances across providers, so a non-200 or dead
        connection on the first attempt often just means a flaky backend
        was picked, and a fresh attempt lands elsewhere. Once streaming has
        actually started (content yielded), we never retry from scratch —
        that would duplicate text the caller may have already shown.

        Every raw line from the network is explicitly decoded as UTF-8 so that
        Persian/multi-byte characters are never corrupted (mojibake)."""
        payload = {
            "model": CHAT_MODEL,
            "messages": messages,
            "stream": True,
        }
        attempts = CHAT_RETRY_ATTEMPTS + 1
        for attempt in range(1, attempts + 1):
            try:
                with self.session.post(
                    f"{self.chat_base}/chat/completions",
                    json=payload,
                    headers=self.chat_headers_fn(),
                    stream=True,
                    timeout=200,
                ) as r:
                    if r.status_code != 200:
                        try:
                            body = r.text[:500]
                        except Exception:
                            body = "<unreadable>"
                        log.warning(
                            "stream_chat non-200 (attempt %d/%d): %d — %s",
                            attempt, attempts, r.status_code, body,
                        )
                        if attempt < attempts:
                            time.sleep(0.6 * attempt)
                            continue
                        yield ("status", r.status_code)
                        return

                    yielded_any = False
                    try:
                        for raw in r.iter_lines(decode_unicode=False):
                            if not raw:
                                continue
                            if isinstance(raw, bytes):
                                line = raw.decode("utf-8", errors="replace").strip()
                            else:
                                line = str(raw).strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:") :].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                delta = chunk["choices"][0].get("delta", {})
                                piece = delta.get("content")
                                if piece:
                                    yielded_any = True
                                    yield ("delta", piece)
                            except Exception:
                                continue
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ReadTimeout,
                        urllib3.exceptions.ProtocolError,
                        ConnectionError,
                    ) as stream_err:
                        if not yielded_any and attempt < attempts:
                            log.warning(
                                "stream_chat: connection dropped before any content (attempt %d/%d): %s — retrying",
                                attempt, attempts, stream_err,
                            )
                            time.sleep(0.6 * attempt)
                            continue
                        log.warning("stream_chat: connection dropped mid-stream: %s", stream_err)
                        yield ("error", "stream_dropped")
                        return
                return
            except requests.exceptions.RequestException as req_err:
                log.warning(
                    "stream_chat: request failed before streaming (attempt %d/%d): %s",
                    attempt, attempts, req_err,
                )
                if attempt < attempts:
                    time.sleep(0.6 * attempt)
                    continue
                yield ("error", "connect_failed")
                return

    def generate(self, messages):
        """One-shot non-streaming chat call. Returns text or '' on failure."""
        return self._chat_completion_with_retry(
            {"model": CHAT_MODEL, "messages": messages, "stream": False},
            timeout=180,
        )

    def generate_image(self, prompt):
        r = self.session.post(
            f"{self.image_base}/images/generations",
            json={
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "size": "1024x1024",
                "extra_body": {"response_format": "url"},
            },
            headers=self.image_headers_fn(),
            timeout=90,
        )
        return r


agnes = AgnesClient(HTTP, CHAT_API_BASE, AGNES_BASE, _chat_headers, _image_headers)
