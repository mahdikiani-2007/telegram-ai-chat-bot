"""Mechanical module extractor: moves verbatim line spans from bot.py into
new modules, replacing each span with an explicit `from <mod> import ...`.

Usage: python3 tools/extract.py   (specs are in SPECS below; edit per step)

Safety:
  * asserts spans sit on blank-line boundaries (statement-safe cuts)
  * auto-computes the import list = names defined in span AND used outside it
  * static unresolved-name check on every new module after writing
  * exits non-zero on any problem; writes nothing until all checks pass
"""
import ast
import builtins
import sys

SRC = "bot.py"
BUILTINS = set(dir(builtins)) | {"__name__", "__file__"}

# Each spec: explicit (start,end) spans and/or top-level AST `nodes`
# (function/class/assign names; banners + decorators absorbed automatically).
# DONE (already extracted — record only, see git history of this file):
#   runs 1-4b: config, timeutil, sessions, infra, limits, metrics,
#     db, ai, bots, memory, search, digest, scheduling, backup, debate,
#     streaming, media
#   run 5a: sessions+=_session_gc_loop, handlers.shared, handlers.guard
#   run 5b: services.streaming+=sanitize_backtick_span,
#           handlers.admin, handlers.tasks; _START_TIME -> metrics.py
#   run 5c: services.streaming+=rate_limited_reply,
#           services.memory+=SYSTEM_PROMPT_LIGHT/build_context_light,
#           handlers.basics/surfaces/medialink/memory_cmds/chat,
#           handlers/debate.py (handle_stopall, manual move)
SPECS = [
    dict(mod="wiring", nodes=[
        "_ig_relay_deps",
        "POLLING_ALLOWED_UPDATES",
        "_sync_bot_commands",
        "run_bot_l", "run_bot_light",
    ], header=(
        "import logging\n"
        "import threading\n"
        "import time\n"
        "import traceback\n"
        "\n"
        "import ig_relay\n"
        "from telebot import types\n"
        "\n"
        "from bots import admin_notifier, bot, bot_light\n"
        "from config import ADMIN_ID, DB_FILE, TELEGRAM_UPLOAD_MAX_BYTES\n"
        "from db import repo\n"
        "from metrics import METRICS\n"
        "from services.media import download_instagram_post, extract_instagram_urls\n"
        "\n"
        'log = logging.getLogger("L-bot")\n'
        "\n"
        "\n"
        "def register_all():\n"
        "    # Import handler modules in dispatch order (telebot\n"
        "    # first-match-wins) and register the relay command. Must run\n"
        "    # before polling starts; safe to call once.\n"
        "    import handlers.guard  # noqa: F401  (foreign commands + middleware first)\n"
        "    ig_relay.register(bot=bot, admin_id=ADMIN_ID, get_deps=_ig_relay_deps)\n"
        "    import handlers.admin  # noqa: F401\n"
        "    import handlers.tasks  # noqa: F401\n"
        "    import handlers.basics  # noqa: F401\n"
        "    import handlers.surfaces  # noqa: F401\n"
        "    import handlers.debate  # noqa: F401\n"
        "    import handlers.medialink  # noqa: F401\n"
        "    import handlers.memory_cmds  # noqa: F401\n"
        "    import handlers.chat  # noqa: F401  (catch-alls register last)\n"
    )),
    dict(mod="main", spans=[(878, -1)], no_import=True, header=(
        "import logging\n"
        "import os\n"
        "import shutil\n"
        "import threading\n"
        "import time\n"
        "\n"
        "import ig_relay\n"
        "import yt_dlp\n"
        "\n"
        "from bots import (\n"
        "    bot,\n"
        "    bot_light,\n"
        "    get_bot_identity,\n"
        "    get_bot_light_identity,\n"
        ")\n"
        "from config import (\n"
        "    BACKUP_HOUR_IRAN,\n"
        "    DB_FILE,\n"
        "    INSTAGRAM_COOKIES_FILE,\n"
        "    YOUTUBE_COOKIES_FILE,\n"
        ")\n"
        "from services.backup import _backup_scheduler_loop\n"
        "from services.digest import _digest_scheduler_loop\n"
        "from services.media import _userbot_thread_main\n"
        "from services.memory import (\n"
        "    BEHAVIOR_PROFILE_HOUR,\n"
        "    _behavior_profiler_loop,\n"
        "    fact_extractor,\n"
        ")\n"
        "from services.scheduling import _reminder_scheduler_loop\n"
        "from sessions import _session_gc_loop\n"
        "from wiring import (\n"
        "    _sync_bot_commands,\n"
        "    register_all,\n"
        "    run_bot_l,\n"
        "    run_bot_light,\n"
        ")\n"
        "\n"
        'log = logging.getLogger("L-bot")\n'
    )),
]
# (scheduling/backup/debate/streaming/db-append specs were here — run 4a)


def top_defined(tree):
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.append(node.target.id)
    return names


def loaded_names(tree):
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def imported_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
    return names


def resolve_nodes(tree, lines, names):
    """Map top-level names to (start, end) spans, absorbing col-0 comment
    banners and decorators directly attached above. A def directly inside
    `if bot_light:` climbs to the enclosing If so the guard moves with it."""
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    by_name = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            by_name[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    by_name.setdefault(t.id, node)
    # nested defs (e.g. light variants inside `if bot_light:`) resolve too;
    # the If-climb below lifts them with their guard
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            by_name.setdefault(node.name, node)
    spans = []
    for name in names:
        if name not in by_name:
            sys.exit(f"node {name!r} not found as a top-level def")
        node = by_name[name]
        while parent.get(node) is not None and isinstance(
                parent[node], ast.If):
            node = parent[node]
        start = node.lineno
        end = node.end_lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.decorator_list:
            start = min([start] + [d.lineno for d in node.decorator_list])
        while start - 1 >= 1 and lines[start - 2].startswith("#") \
                and not lines[start - 2].startswith("# (moved to"):
            start -= 1
        spans.append((start, end))
    # merge nested/duplicate spans from If-climbing within one module
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def mod_path(mod):
    return mod.replace(".", "/") + ".py"


def main():
    with open(SRC, encoding="utf-8") as f:
        lines = f.readlines()
    n = len(lines)

    full_src = "".join(lines)
    full_tree = ast.parse(full_src)

    # expand node selections into concrete spans
    expanded = []
    for s in SPECS:
        spans = [(a, n if b == -1 else b) for (a, b) in s.get("spans", [])]
        if s.get("nodes"):
            spans.extend(resolve_nodes(full_tree, lines, s["nodes"]))
        if not spans:
            sys.exit(f"spec for {s['mod']} has no spans/nodes — refusing to silently skip")
        for (a, b) in spans:
            assert 1 <= a <= b <= n, f'{s["mod"]}: span out of range'
            expanded.append(dict(mod=s["mod"], start=a, end=b,
                                 header=s.get("header"),
                                 append_only=bool(s.get("append_only")),
                                 no_import=bool(s.get("no_import")),
                                 explicit=(a, b) in [tuple(x) for x in s.get("spans", [])]))

    for e in expanded:
        if not e["explicit"]:
            continue
        a, b = e["start"], e["end"]
        # statement-exactness: must align with top-level AST node boundaries
        starts = {node.lineno for node in full_tree.body
                  if hasattr(node, "lineno")}
        ends = {node.end_lineno for node in full_tree.body
                if hasattr(node, "end_lineno")}
        assert a in starts, f'{e["mod"]}: line {a} is not a statement start'
        assert b in ends, f'{e["mod"]}: line {b} is not a statement end'

    # spans of different modules must not overlap
    occ = {}
    for e in expanded:
        for ln in range(e["start"], e["end"] + 1):
            if ln in occ and occ[ln] != e["mod"]:
                sys.exit(f"overlap: line {ln} claimed by {occ[ln]} and {e['mod']}")
            occ[ln] = e["mod"]

    # loads occurring outside each module's spans
    uses_outside = {m: set() for m in {e["mod"] for e in expanded}}
    span_of_line = {ln: e["mod"] for e in expanded
                    for ln in range(e["start"], e["end"] + 1)}
    for node in ast.walk(full_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            owner = span_of_line.get(node.lineno)
            for m in uses_outside:
                if m != owner:
                    uses_outside[m].add(node.id)

    planned = []
    # group spans by module (multi-span modules concatenate in file order)
    mods = list(dict.fromkeys(e["mod"] for e in expanded))
    groups = {m: sorted([e for e in expanded if e["mod"] == m],
                        key=lambda e: e["start"]) for m in mods}
    for m in mods:
        parts = []
        defined = []
        for e in groups[m]:
            span_src = "".join(lines[e["start"] - 1:e["end"]])
            parts.append(span_src)
            defined.extend(top_defined(ast.parse(span_src)))
        new_defined = list(dict.fromkeys(defined))
        if any(e.get("append_only") for e in groups[m]):
            # existing module content already defines/imports names
            with open(mod_path(m), encoding="utf-8") as f:
                existing_tree = ast.parse(f.read())
            defined.extend(top_defined(existing_tree))
            pre_imports = imported_names(existing_tree)
        else:
            pre_imports = set()
        # intra-module reassignment is fine — dedupe, first-seen order
        defined = list(dict.fromkeys(defined))
        no_imp = any(e.get("no_import") for e in groups[m])
        assert defined or no_imp, f'{m}: no top-level names defined!'
        # need: only NEWLY moved names used elsewhere (pre-existing names
        # stay out so appends don't drag header names like `log` along)
        need = sorted(set(new_defined) & uses_outside[m])
        planned.append((m, groups[m], "\n\n".join(
            p.rstrip("\n") for p in parts) + "\n", defined, need, pre_imports,
            parts))

    # cross-module check: every load inside a new module must resolve
    ok = True
    headers = {}
    for m in mods:
        hs = [e["header"] for e in groups[m]
              if e.get("header")]
        if any(e.get("append_only") for e in groups[m]):
            with open(mod_path(m), encoding="utf-8") as f:
                existing = f.read()
            headers[m] = existing.split("\n\n\n", 1)[0] if "\n\n\n" in existing else ""
        else:
            # one header per module (spans share their spec's header)
            headers[m] = next((h for h in hs if h), "")
    for m, spans, span_src, defined, need, pre_imports, parts in planned:
        header = headers[m]
        header_tree = ast.parse(header) if header.strip() else None
        have_imports = imported_names(header_tree) if header_tree else set()
        have_defined = top_defined(header_tree) if header_tree else []
        # module-level names available at runtime: own defines + header
        # defines/imports (+ lazy imports inside function bodies).
        # Anything else it loads must come from bot.py or
        # another new module and is a LEAK unless explicitly imported.
        span_tree = ast.parse(span_src)
        body_imports = imported_names(span_tree)
        body_loads = loaded_names(span_tree) - set(defined) \
            - set(have_defined) - have_imports - body_imports \
            - pre_imports - BUILTINS
        # names that remain in bot.py after extraction
        remaining_defs = set()
        cut = set()
        for m2 in mods:
            for e in groups[m2]:
                cut.update(range(e["start"], e["end"] + 1))
        for node in full_tree.body:
            ln = getattr(node, "lineno", 0)
            if ln and ln not in cut:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    remaining_defs.add(node.name)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            remaining_defs.add(t.id)
        # original top imports (stdlib/third-party) available to bot.py only —
        # new modules must import those themselves
        top_imports = imported_names(full_tree)
        other_mods = {}
        for om in mods:
            defs = []
            for e in groups[om]:
                defs.extend(top_defined(ast.parse(
                    "".join(lines[e["start"] - 1:e["end"]]))))
            other_mods[om] = defs
        for name in sorted(body_loads):
            if name in remaining_defs or name in top_imports:
                print(f"LEAK {m}: uses {name!r} from bot.py/original imports "
                      f"without importing it")
                ok = False
            elif any(name in defs for om, defs in other_mods.items() if om != m):
                print(f"LEAK {m}: uses {name!r} defined in another new module "
                      f"without importing it")
                ok = False
            # else: param/local/attr-base/comprehension — fine
    if not ok:
        sys.exit("extract aborted: unresolved names (see LEAK lines)")

    # write new modules
    import os
    for m, spans, span_src, defined, need, pre_imports, parts in planned:
        header = headers[m]
        body = span_src.rstrip("\n") + "\n"
        if any(e.get("append_only") for e in spans):
            with open(mod_path(m), encoding="utf-8") as f:
                existing = f.read().rstrip("\n")
            # new spans' defines (parts only, not pre-existing file content)
            new_defined = list(dict.fromkeys(
                d for part in parts
                for d in top_defined(ast.parse(part.rstrip("\n") + "\n"))))
            existing_defs = set(top_defined(ast.parse(existing + "\n")))
            if all(d in existing_defs for d in new_defined):
                print(f"skip {mod_path(m)}: append content already present")
                continue
            content = existing + "\n\n\n" + body
        else:
            # normalize: header, blank lines, body
            content = header.rstrip("\n") + "\n\n\n" + body if header else body
            parent = os.path.dirname(mod_path(m))
            if parent:
                os.makedirs(parent, exist_ok=True)
                init = os.path.join(parent, "__init__.py")
                if not os.path.exists(init):
                    with open(init, "w", encoding="utf-8") as f:
                        f.write("")
        with open(mod_path(m), "w", encoding="utf-8") as f:
            f.write(content)
        no_import = any(e.get("no_import") for e in spans)
        print(f'wrote {mod_path(m)} ({len(defined)} defines, '
              f'imports {need if need else "NONE (no_import)" if no_import else "NONE — side-effect only!"}')
        if not need and not no_import:
            sys.exit(f'abort: {m} has no external users; refusing to orphan code')

    # rewrite bot.py from last span to first; only the first span of each
    # module gets the import, later spans become pointer comments.
    # no_import modules (handler registration lives in wiring import order):
    # all spans become pointer comments, no import line is added.
    # If bot.py already imports the module, merge new names into that line.
    import re
    first = {m: min(e["start"] for e in groups[m]) for m in mods}
    flat = sorted([e for e in groups for e in groups[e]],
                  key=lambda e: -e["start"])
    need_of = {m: n for m, _, _, _, n, _, _ in planned}
    no_import_of = {m: any(x.get("no_import") for x in groups[m])
                    for m in mods}
    for e in flat:
        m = e["mod"]
        if e["start"] == first[m] and not no_import_of[m]:
            lines[e["start"] - 1:e["end"]] = [
                f"from {m} import ({', '.join(need_of[m])})\n"]
        else:
            lines[e["start"] - 1:e["end"]] = [f"# (moved to {m}.py)\n"]
    text = "".join(lines)
    # merge every `from <mod> import (...)` line per module into one,
    # kept at the first occurrence position (stable import order)
    work = text.splitlines(keepends=True)
    for m in mods:
        if no_import_of[m]:
            continue
        pat = re.compile(rf"^from {re.escape(m)} import \((.*)\)$")
        names = set(need_of[m])
        first_idx = None
        for i, ln in enumerate(work):
            if ln is None:
                continue
            mm = pat.match(ln.rstrip("\n"))
            if mm:
                names.update(n.strip() for n in mm.group(1).split(",")
                             if n.strip())
                if first_idx is None:
                    first_idx = i
                work[i] = None
        if first_idx is not None and names:
            work[first_idx] = f"from {m} import ({', '.join(sorted(names))})\n"
    text = "".join(ln for ln in work if ln is not None)
    with open(SRC, "w", encoding="utf-8") as f:
        f.write(text)
    print("bot.py rewritten")


if __name__ == "__main__":
    main()
