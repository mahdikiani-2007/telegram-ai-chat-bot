"""Handler registration-order guard for the handlers extraction phase.

The strict census compares one global handler sequence, which legitimately
changes shape when handlers move across files. This tool checks what
actually matters for telebot first-match-wins dispatch:

  1. handler multiset identical to baseline (kind+func+commands+prefix+text)
  2. per-file internal relative order preserved vs baseline
  3. wiring.register_all() imports handler modules guard-first, chat-last
  4. no handlers/* module imports another handlers/* module except
     handlers.shared (shared has no decorators, so import order is safe)

Usage: python3 tools/check_order.py [--baseline tests/baseline_inventory.json]
"""
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from census import scan_dir  # noqa: E402


def key(h):
    return (h["bot"], h["kind"], h["func"], h["commands"] or "",
            h["data_prefix"] or "", h["text_match"] or "")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="tests/baseline_inventory.json")
    ap.add_argument("--dir", default=".")
    args = ap.parse_args()

    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    # baseline stores rendered handler strings in the same format scan_dir makes
    base_keys = base["handler_order"]
    inv = scan_dir(args.dir)
    cur_entries = [h for f in inv["files"].values() for h in f.get("handlers", [])]
    cur_rendered = [
        f"{h['bot']}.{h['kind']}:{h['func']}:{h['commands'] or ''}"
        f":{h['data_prefix'] or ''}:{h['text_match'] or ''}"
        for h in cur_entries]

    ok = True
    if sorted(base_keys) != sorted(cur_rendered):
        ok = False
        print("MISMATCH handler multiset:")
        for x in sorted(set(cur_rendered) - set(base_keys)):
            print(f"  + {x}")
        for x in sorted(set(base_keys) - set(cur_rendered)):
            print(f"  - {x}")

    # per-file internal order preserved
    base_pos = {k: i for i, k in enumerate(base_keys)}
    by_file = {}
    for h, r in zip(cur_entries, cur_rendered):
        by_file.setdefault(h["file"], []).append(r)
    for fn, seq in sorted(by_file.items()):
        poss = [base_pos[k] for k in seq]
        if poss != sorted(poss):
            ok = False
            print(f"ORDER BROKEN inside {fn}")

    # wiring import order: guard first, chat last
    wiring = os.path.join(args.dir, "wiring.py")
    if os.path.exists(wiring):
        src = open(wiring, encoding="utf-8").read()
        order = re.findall(r"import\s+(handlers\.\w+)", src)
        order = list(dict.fromkeys(order))
        if order:
            if order[0] != "handlers.guard":
                ok = False
                print(f"wiring: first handler import is {order[0]}, want handlers.guard")
            if order[-1] != "handlers.chat":
                ok = False
                print(f"wiring: last handler import is {order[-1]}, want handlers.chat")
            print("wiring handler import order:", " -> ".join(order))
    else:
        print("(no wiring.py yet — import-order check deferred)")

    # no handlers->handlers imports except via handlers.shared
    for rel in sorted(inv["files"]):
        if not rel.startswith("handlers/") or rel == "handlers/shared.py":
            continue
        if rel.startswith("handlers/") and rel.count("/") == 1:
            src = open(os.path.join(args.dir, rel), encoding="utf-8").read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module == "handlers.shared" or node.module == "handlers":
                        continue
                    if node.module.startswith("handlers."):
                        ok = False
                        print(f"CROSS-IMPORT {rel} -> {node.module} "
                              f"(risks decorator mis-ordering)")
    print("ORDER " + ("OK" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
