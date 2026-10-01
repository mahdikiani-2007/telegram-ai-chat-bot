"""Pytest-first hermetic env: runs before ANY test module is imported, so
no bot import can ever bind real credentials or the real DATA_DIR.

Do NOT set these in individual test modules (too late — whichever module
pytest imports first would win for singletons like db.repo).
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="lbot_tests_")
os.environ["TELEGRAM_TOKEN"] = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
os.environ["AGNES_API_KEY"] = "test-dummy-key"
os.environ.pop("TELEGRAM_TOKEN_LIGHT", None)
os.environ.pop("DROP_PENDING_UPDATES", None)
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
