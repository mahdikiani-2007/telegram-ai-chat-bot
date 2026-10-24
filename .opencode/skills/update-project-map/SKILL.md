---
name: update-project-map
description: Keep PROJECT_MAP.md and docs/map/ in sync after creating, modifying, renaming, moving, or deleting project code, dependencies, env vars, handlers, DB schema, or docs layout.
---

# Update Project Map

Run this skill at the END of every task that changed this project — new/edited/renamed/moved/deleted files, new dependencies, new env vars, DB schema changes, new or removed handlers/commands/callbacks/jobs, changed flows — before telling the user the task is done.

## Procedure

1. **Determine what changed.** Prefer `git diff --stat` + `git status --short`. If git is unavailable, use the list of files touched in this session.
2. **Open `PROJECT_MAP.md`** (and the relevant `docs/map/` files: `modules.md`, `data.md`, `integrations.md`, `config.md`) and update **ONLY the affected sections**: directory tree, feature/handler index, modules reference, data layer, state, integrations, config/env vars, "where do I change X?" lookup, conventions/gotchas, and the `Last verified:` line (set to today's date + one-line note of what changed).
3. **Remove entries for deleted or renamed things.** Never leave stale entries: a renamed handler, moved function, dropped table/column, or removed env var must disappear from every section that mentioned it.
4. **Verify every changed entry against the actual code** (grep for the names/paths) before finishing. If map and code disagree, trust the code and fix the map.
5. **Keep the map's format, style, and rules:** paths relative to project root, tables and bullets over prose, no code dumps, **no line numbers anywhere**, and **never write secret values, tokens, or keys** (names of env vars only).
6. **Large change** (new module, big refactor, schema overhaul): re-verify the whole map instead of only the changed parts — re-check every file path, function name, handler, env var, and DB field, and confirm no project file is missing from the directory tree and no handler is missing from the feature index.
7. **Size budget:** `PROJECT_MAP.md` must stay under ~500 lines. When adding entries, also remove or condense low-value ones. If a split into `docs/map/` files exists, update the correct file and keep the index links in `PROJECT_MAP.md` valid.

## When finished

- Tell the user in 2–3 lines which map sections were updated. If nothing needed updating, say so explicitly.
- If `PROJECT_MAP.md` is missing, do not invent one from memory: tell the user it is missing and offer to regenerate it fully (explore every source file, config, dependency, env example, deployment file, schema, test, and script; write only what the code shows, mark the rest `UNCLEAR:`; same sections, same budget, same no-line-numbers rule).
