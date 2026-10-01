# Backup & restore

## What exists and where
- **Local snapshots**: `DATA_DIR/backups/chat_history_YYYY-MM-DD_HHMM.db.gz`,
  taken daily ~04:00 Iran time, newest `BACKUP_KEEP_DAILY` (default 7) kept,
  older pruned automatically.
- **Off-site copy**: the same archive is DM'd to `ADMIN_ID` daily (belt and
  suspenders for a wiped volume).
- Snapshots are taken **after** the daily maintenance (90-day message purge
  by default, weekly `VACUUM`), so they stay small. Summaries, todos,
  reminders, sessions, facts, and profiles are never purged.

## Verify an archive (do this before trusting it)
```bash
python3 tools/verify_restore.py --archive DATA_DIR/backups/<file>.db.gz
```
Checks gzip integrity, `PRAGMA integrity_check`, and core-table presence.
Exit 0 = healthy. Anything else = do not deploy that archive.

## Restore procedure (Railway volume / local disk)
1. **Stop the bot** (Railway: stop the service; local: kill the process).
   Never replace the DB under a running bot.
2. Verify the archive with the command above.
3. Restore it:
   ```bash
   python3 tools/verify_restore.py \
       --archive DATA_DIR/backups/<file>.db.gz \
       --restore /data/chat_history.db
   ```
   (`--restore` re-verifies, places the file, and removes stale
   `-wal`/`-shm`/`-journal` sidecars that could resurrect old pages.)
4. **Restart the bot.** Missing columns/indexes self-migrate on boot;
   watch the first minute of logs for errors.

## Tuning knobs (env vars)
| Var | Default | Meaning |
|---|---|---|
| `DB_RETENTION_DAYS` | 90 | conversational history kept (`messages` only) |
| `DB_SIZE_WARN_MB` | 500 | admin alert threshold for DB file size (0 = off) |
| `BACKUP_KEEP_DAILY` | 7 | local snapshots retained |

## Testing the path
`tests/test_backup.py` covers purge, VACUUM gating, snapshot pruning,
and a build→verify→restore roundtrip: `python3 -m pytest tests/ -q`.
