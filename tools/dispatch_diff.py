"""Compare two dispatch dumps (see dump_dispatch.py).

Compares, per bot instance and handler stream:
  1. registration ORDER + function names + commands/content_types filters
  2. handler function bodies (AST, decorator-insensitive... no:
     decorator-SENSITIVE full-source AST, since decorators ARE dispatch)

Exits non-zero on any difference.
Usage: python3 tools/dispatch_diff.py base.json new.json
"""
import json
import sys


def main():
    base = json.load(open(sys.argv[1]))
    new = json.load(open(sys.argv[2]))
    ok = True
    for label in ("bot", "bot_light"):
        bt, nt = base["bots"].get(label), new["bots"].get(label)
        if bt is None and nt is None:
            continue
        if (bt is None) != (nt is None):
            print(f"MISMATCH {label}: present in one side only")
            ok = False
            continue
        if set(bt) != set(nt):
            print(f"MISMATCH {label} streams: {sorted(set(bt) ^ set(nt))}")
            ok = False
        for stream in sorted(set(bt) & set(nt)):
            brows, nrows = bt[stream], nt[stream]
            if len(brows) != len(nrows):
                print(f"MISMATCH {label}.{stream}: "
                      f"{len(brows)} vs {len(nrows)} rows")
                ok = False
            for i, (b, n) in enumerate(zip(brows, nrows)):
                if b["func"] != n["func"] or b["filters"] != n["filters"]:
                    print(f"MISMATCH {label}.{stream}[{i}]:\n"
                          f"  base={b['func']} {b['filters']}\n"
                          f"  new ={n['func']} {n['filters']}")
                    ok = False
                if b["body_ast"] != n["body_ast"]:
                    print(f"MISMATCH {label}.{stream}[{i}] {b['func']}: "
                          f"handler source differs")
                    ok = False
            if len(nrows) > len(brows):
                for n in nrows[len(brows):]:
                    print(f"EXTRA {label}.{stream}: {n['func']} {n['filters']}")
                    ok = False
    print("DISPATCH " + ("OK — tables identical" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
