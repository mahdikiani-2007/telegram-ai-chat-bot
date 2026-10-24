"""Live SIGTERM probe for Phase 4.1 (test-only harness, not shipped).

Simulates main()'s tail with stubbed network: one run_bot_l-style polling
thread blocked in a fake long-poll, real signal handlers, real shutdown
sequence. Sends SIGTERM to itself and must exit 0 with all markers.
"""
import os
import signal
import sys
import tempfile
import threading

os.environ["TELEGRAM_TOKEN"] = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
os.environ["AGNES_API_KEY"] = "probe-dummy-key"
os.environ.pop("TELEGRAM_TOKEN_LIGHT", None)
os.environ["DATA_DIR"] = os.path.join(tempfile.mkdtemp(prefix="lbot_probe_"), "data")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bots
from wiring import _request_shutdown, _shutdown_sequence, shutdown_event

stop_flag = threading.Event()
calls = []

bots.bot.delete_webhook = lambda *a, **k: calls.append("delete_webhook")
bots.bot.infinity_polling = lambda *a, **k: (
    calls.append("poll-enter"),
    print("probe: polling entered", flush=True),
    stop_flag.wait(30),
    calls.append("poll-return"),
)[-1]


def fake_stop_polling():
    calls.append("stop_polling")
    stop_flag.set()


bots.bot.stop_polling = fake_stop_polling

signal.signal(signal.SIGTERM, _request_shutdown)
signal.signal(signal.SIGINT, _request_shutdown)
print("probe: handlers installed", flush=True)

t = threading.Thread(target=__import__("wiring").run_bot_l,
                     daemon=True, name="probe-polling")
t.start()
if not stop_flag.wait(0.1):  # let the polling thread enter fake_poll first
    import time as _t
    _t.sleep(1)

os.kill(os.getpid(), signal.SIGTERM)

while not shutdown_event.is_set():
    shutdown_event.wait(60)

_shutdown_sequence([t], grace_seconds=1)
print("probe: clean exit order=" + ",".join(calls), flush=True)
sys.exit(0)
