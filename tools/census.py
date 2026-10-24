"""Static inventory of Telegram handlers, DB tables, env vars and threads.

AST/regex based — does NOT import the bot (no env or network needed).
Run:  python3 tools/census.py [--dir PATH] [--save PATH] [--compare PATH]

Contract: after each refactor step the inventory must be identical
(except `sources`, which records where each handler lives now).
"""
import ast
import json
import os
import re
import sys

HANDLER_DEC_RE = re.compile(
    r"@(bot|bot_light)\.(message_handler|callback_query_handler|middleware_handler)\b(.*)"
)
REGISTER_CALL_RE = re.compile(
    r"(bot|bot_light)\.register_(inline_handler|chosen_inline_handler|guest_message_handler)\b"
)
COMMANDS_RE = re.compile(r"commands\s*=\s*(\[[^\]]*\])")
DATA_PREFIX_RE = re.compile(r"""startswith\(\s*["']([^"']+)["']\s*\)""")
CONTENT_TYPES_RE = re.compile(r"content_types\s*=\s*(\[[^\]]*\])")
CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)
ENV_GET_RE = re.compile(r"""os\.environ\.get\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")
THREAD_NAME_RE = re.compile(r"""name\s*=\s*["']([^"']+)["']""")
FUNC_FILTER_RE = re.compile(r"func\s*=\s*([^\n,]+(?:\([^)]*\))?)")
TEXT_MATCH_RE = re.compile(r"""m\.text\s*==\s*["']([^"']+)["']""")


def _decorator_detail(lines, idx):
    """Join a (possibly multi-line) decorator starting at line idx (0-based)."""
    buf = lines[idx].strip()
    depth = buf.count("(") - buf.count(")")
    j = idx
    while depth > 0 and j + 1 < len(lines):
        j += 1
        line = lines[j].strip()
        # safety: a new decorator/def starts — previous one was unbalanced
        if line.startswith("@") or line.startswith("def "):
            break
        depth += line.count("(") - line.count(")")
        buf += " " + line
    return buf


def _handler_name(lines, idx):
    for j in range(idx + 1, min(idx + 6, len(lines))):
        m = re.match(r"\s*def\s+(\w+)\s*\(", lines[j])
        if m:
            return m.group(1), j + 1
    return "<unknown>", idx + 1


def scan_file(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    handlers = []
    for i, line in enumerate(lines):
        m = HANDLER_DEC_RE.match(line.strip())
        if m:
            bot_obj, kind, _ = m.groups()
            detail = _decorator_detail(lines, i)
            name, _ = _handler_name(lines, i)
            handlers.append({
                "file": os.path.basename(path),
                "line": i + 1,
                "bot": bot_obj,
                "kind": kind,
                "func": name,
                "commands": (COMMANDS_RE.search(detail).group(1)
                             if COMMANDS_RE.search(detail) else None),
                "data_prefix": (DATA_PREFIX_RE.search(detail).group(1)
                                if DATA_PREFIX_RE.search(detail) else None),
                "content_types": (CONTENT_TYPES_RE.search(detail).group(1)
                                  if CONTENT_TYPES_RE.search(detail) else None),
                "text_match": (TEXT_MATCH_RE.search(detail).group(1)
                               if TEXT_MATCH_RE.search(detail) else None),
            })
        m2 = REGISTER_CALL_RE.search(line)
        if m2:
            bot_obj, kind = m2.groups()
            handlers.append({
                "file": os.path.basename(path),
                "line": i + 1,
                "bot": bot_obj,
                "kind": "register_" + kind,
                "func": "<lambda>",
                "commands": None,
                "data_prefix": None,
                "content_types": None,
                "text_match": None,
            })
    try:
        tree = ast.parse(src)
        funcs = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    except SyntaxError as e:
        return {"path": path, "syntax_error": str(e)}
    return {
        "handlers": handlers,
        "top_functions": len(funcs),
        "tables": sorted(set(CREATE_TABLE_RE.findall(src))),
        "env_vars": sorted(set(ENV_GET_RE.findall(src))),
        "thread_names": sorted(set(THREAD_NAME_RE.findall(src))),
    }


def scan_dir(root, exclude=()):
    inv = {"files": {}, "handler_order": []}
    for dirpath, _, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if "__pycache__" in dirpath or "/tests" in dirpath or "/tools" in dirpath:
            continue
        if rel_dir.split(os.sep)[0] in exclude:
            continue
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            data = scan_file(full)
            inv["files"][rel] = data
            for h in data.get("handlers", []):
                inv["handler_order"].append(
                    f"{h['bot']}.{h['kind']}:{h['func']}:{h['commands'] or ''}"
                    f":{h['data_prefix'] or ''}:{h['text_match'] or ''}")
    return inv


def normalize(inv):
    """Order-independent comparison key (sources may move across files)."""
    return {
        "handler_order": inv["handler_order"],
        "tables": sorted({t for f in inv["files"].values()
                          for t in f.get("tables", [])}),
        "env_vars": sorted({e for f in inv["files"].values()
                            for e in f.get("env_vars", [])}),
        "thread_names": sorted({t for f in inv["files"].values()
                                for t in f.get("thread_names", [])}),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--exclude", default="", help="comma-separated top-level dirs to skip")
    ap.add_argument("--save", default=None)
    ap.add_argument("--compare", default=None)
    args = ap.parse_args()
    exclude = tuple(e for e in args.exclude.split(",") if e)
    inv = scan_dir(args.dir, exclude)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(inv, f, indent=1, ensure_ascii=False)
        print(f"saved {args.save}: {len(inv['handler_order'])} handlers, "
              f"{len(inv['files'])} files")
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            base = json.load(f)
        a, b = normalize(base), normalize(inv)
        ok = True
        for key in ("handler_order", "tables", "env_vars", "thread_names"):
            if a[key] != b[key]:
                ok = False
                print(f"MISMATCH {key}:")
                sa, sb = set(a[key]), set(b[key])
                for x in sorted(sb - sa):
                    print(f"  + {x}")
                for x in sorted(sa - sb):
                    print(f"  - {x}")
                if key == "handler_order":
                    # show first order divergence
                    for i, (x, y) in enumerate(zip(a[key], b[key])):
                        if x != y:
                            print(f"  order diverges at #{i}: base={x} new={y}")
                            break
                    if len(a[key]) != len(b[key]):
                        print(f"  lengths differ: base={len(a[key])} new={len(b[key])}")
        print("CENSUS " + ("OK — inventories match" if ok else "FAILED"))
        sys.exit(0 if ok else 1)
    if not args.save:
        n = normalize(inv)
        print(json.dumps({k: len(v) for k, v in n.items()
                          | {"files": list(inv["files"])}.items()}, indent=1))


if __name__ == "__main__":
    main()
