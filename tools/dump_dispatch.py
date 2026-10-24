"""Dump telebot dispatch tables + handler sources as JSON.

Usage: python3 tools/dump_dispatch.py --root DIR --out FILE [--with-light]

Imports `bot` from DIR (stub env, temp DATA_DIR), plus any present
handlers.* modules in canonical wiring order, then serializes every
*handlers* attribute of both bot instances.
"""
import argparse
import ast
import inspect
import json
import os
import sys
import tempfile

ORDER = ["handlers.guard", "handlers.admin", "handlers.tasks",
         "handlers.basics", "handlers.surfaces", "handlers.debate",
         "handlers.medialink", "handlers.memory_cmds", "handlers.chat"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--with-light", action="store_true")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="lbot_dump_")
    os.environ["TELEGRAM_TOKEN"] = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    os.environ["AGNES_API_KEY"] = "dump-dummy"
    os.environ["DATA_DIR"] = os.path.join(tmp, "data")
    if args.with_light:
        os.environ["TELEGRAM_TOKEN_LIGHT"] = "654321:XYZ-DEF1234ghIkl-zyx57W2v1u123ew99"
    else:
        os.environ.pop("TELEGRAM_TOKEN_LIGHT", None)

    sys.path.insert(0, args.root)
    for mod in list(sys.modules):
        if mod == "bot" or mod.startswith(("handlers", "services", "bots",
                                            "config", "db", "ai", "infra",
                                            "limits", "metrics", "sessions",
                                            "timeutil", "wiring", "main",
                                            "ig_relay")):
            del sys.modules[mod]
    os.chdir(args.root)
    try:
        import wiring
        wiring.register_all()
    except ImportError:
        # pre-cutover tree: all handlers register at bot.py import time
        import bot  # noqa: E402
    else:
        import bot  # noqa: E402  (shim; importable without side effects)

    bots_mod = sys.modules.get("bots", bot)
    out = {"root": args.root, "bots": {}}
    for label in ("bot", "bot_light"):
        inst = getattr(bots_mod, label, None)
        if inst is None:
            continue
        tables = {}
        for attr in sorted(dir(inst)):
            if "handler" not in attr:
                continue
            try:
                lst = getattr(inst, attr)
            except Exception:
                continue
            if not isinstance(lst, list):
                continue
            rows = []
            for h in lst:
                if not isinstance(h, dict) or "function" not in h:
                    continue
                fn = h["function"]
                filters = {}
                for k, v in (h.get("filters") or {}).items():
                    if k in ("commands", "content_types", "chat_types"):
                        filters[k] = sorted(map(str, v)) if isinstance(
                            v, (list, tuple)) else str(v)
                    else:
                        filters[k] = type(v).__name__
                try:
                    src = inspect.getsource(fn)
                    body = ast.dump(ast.parse(src).body[0])
                except Exception as e:
                    body = f"<unavailable: {e}>"
                rows.append({"func": getattr(fn, "__name__", repr(fn)),
                             "filters": filters, "body_ast": body})
            tables[attr] = rows
        out["bots"][label] = tables
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    n = sum(len(v) for t in out["bots"].values() for v in t.values())
    print(f"dumped {args.root}: {n} handler rows")


if __name__ == "__main__":
    main()
