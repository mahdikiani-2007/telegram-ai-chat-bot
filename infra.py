import logging

import requests
from requests.adapters import HTTPAdapter
from telebot import apihelper
from urllib3.util.retry import Retry

from config import (
    CHAT_API_KEY,
    IMAGE_API_KEY,
    LOCAL_BOT_API_URL,
    TELEGRAM_API_BASE_URL,
    TELEGRAM_FILE_BASE_URL,
)

log = logging.getLogger("L-bot")


# =========================
# Shared HTTP Session (pooling + retries)
# =========================
def _build_session():
    session = requests.Session()
    # TLS verification is left ON (default). It was previously disabled to
    # work around the Cloudflare-fronted Render proxy; talking directly to
    # api.telegram.org / the LLM providers doesn't need that workaround and
    # disabling verification is a real MITM risk, so don't re-add it.
    retry = Retry(
        total=2,
        connect=2,
        read=0,
        backoff_factor=0.5,
        status_forcelist=[502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


HTTP = _build_session()
apihelper.CUSTOM_REQUEST_SENDER = HTTP.request
# Enables telebot's lightweight update-middleware system (used purely to
# track known_chats/group_members on every incoming update — see below).
apihelper.ENABLE_MIDDLEWARE = True
if LOCAL_BOT_API_URL:
    apihelper.API_URL = TELEGRAM_API_BASE_URL
    apihelper.FILE_URL = TELEGRAM_FILE_BASE_URL
    log.info("Using Local Bot API Server at %s (upload cap raised to ~1950MB).", LOCAL_BOT_API_URL)


def _chat_headers():
    return {
        "Authorization": f"Bearer {CHAT_API_KEY}",
        "Content-Type": "application/json",
    }


def _image_headers():
    return {
        "Authorization": f"Bearer {IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }
