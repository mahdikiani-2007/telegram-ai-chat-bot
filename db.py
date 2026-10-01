import sqlite3
import threading

from config import DB_FILE, DIGEST_MEMORY_DAYS


# =========================
# Database Layer (thread-safe repository, thread-local connection pool)
# =========================
class ChatRepository:
    """All SQLite access.

    A write lock still serializes mutations (SQLite only allows one writer
    at a time regardless), but reads/writes now reuse ONE persistent
    connection per worker thread (via threading.local) instead of opening a
    fresh connection on every single call. With telebot's 8 worker threads
    this caps the bot at 8 live connections total instead of thousands of
    short-lived open/close cycles.
    """

    def __init__(self, db_file):
        self.db_file = db_file
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._init()

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_file, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init(self):
        with self._write_lock:
            conn = self._connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    role TEXT,
                    text TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    chat_id TEXT PRIMARY KEY,
                    summary TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS maintenance_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            # ---- Todo and Reminder systems ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    creator_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    priority TEXT DEFAULT 'Normal',
                    category TEXT DEFAULT 'General',
                    status INTEGER DEFAULT 0,
                    deadline REAL DEFAULT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_todos_chat_status
                ON todos(chat_id, status)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    creator_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    mention_type TEXT DEFAULT 'me',
                    recurrence TEXT DEFAULT 'none',
                    trigger_time REAL NOT NULL,
                    status INTEGER DEFAULT 0,
                    deadline REAL DEFAULT NULL
                )
            """)
            # Safely add columns if the table already existed without them.
            for ddl in (
                "ALTER TABLE todos ADD COLUMN deadline REAL DEFAULT NULL",
                "ALTER TABLE todos ADD COLUMN recurrence TEXT DEFAULT 'none'",
                "ALTER TABLE todos ADD COLUMN assignee_id INTEGER DEFAULT NULL",
                "ALTER TABLE todos ADD COLUMN assignee_username TEXT DEFAULT NULL",
                "ALTER TABLE reminders ADD COLUMN deadline REAL DEFAULT NULL",
                "ALTER TABLE reminders ADD COLUMN assignee_id INTEGER DEFAULT NULL",
                "ALTER TABLE reminders ADD COLUMN assignee_username TEXT DEFAULT NULL",
                # Crash-safe dispatch state (Phase 4.3): persistent claim +
                # attempt counters replace the old in-memory sets, so a
                # restart can neither double-fire nor lose track of retries.
                "ALTER TABLE reminders ADD COLUMN attempts INTEGER DEFAULT 0",
                "ALTER TABLE reminders ADD COLUMN claimed_at REAL DEFAULT NULL",
                "ALTER TABLE reminders ADD COLUMN claim_token TEXT DEFAULT NULL",
                "ALTER TABLE todos ADD COLUMN attempts INTEGER DEFAULT 0",
                "ALTER TABLE todos ADD COLUMN claimed_at REAL DEFAULT NULL",
                "ALTER TABLE todos ADD COLUMN claim_token TEXT DEFAULT NULL",
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass  # Column already exists
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_trigger
                ON reminders(status, trigger_time)
            """)

            # ---- Feature 1: real broadcast targets (replaces the mock DB) ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_chats (
                    chat_id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    username TEXT,
                    title TEXT,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- Group-mention workaround: per-member tracking within a group.
            # known_chats is keyed by chat_id (one row per CHAT), which cannot
            # hold multiple members of the same group. This second table adds
            # the per-member granularity actually needed to "tag everyone".
            conn.execute("""
                CREATE TABLE IF NOT EXISTS group_members (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            # ---- Messages the bot sent that are just re-uploaded media
            # (Instagram downloads, etc.) rather than a real conversational
            # reply. Replying to one of these should never reach the AI chat
            # pipeline — the reply is about the media, not something the bot
            # said, so answering it is both irrelevant and a wasted API call.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_media_messages (
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            # ---- Per-user chat sessions (private chats only). Each session
            # is a separate memory_id namespace (messages + rolling summary).
            # active_session_id in user_sessions says which one new messages
            # flow into; NULL means "use the plain user_id memory" (the
            # pre-sessions behaviour). Active_session rows are stored
            # separately from the sessions themselves so /session delete
            # never orphans the pointer.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, user_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON sessions(user_id, created_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_active_sessions (
                    user_id INTEGER PRIMARY KEY,
                    active_session_id TEXT
                )
            """)
            # ---- Digest memory: the bullet lines of each day's digest, kept
            # 7 days, injected into the write-up prompt so the model filters
            # out news it already reported (it was repeating stories daily).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS digest_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest_date TEXT NOT NULL,
                    line TEXT NOT NULL
                )
            """)
            # ---- Schema migrations for tables that gained columns after
            # first deploy. CREATE TABLE IF NOT EXISTS skips pre-existing
            # tables, so new columns would never appear on an old volume —
            # every query touching them would then fail with
            # "no such column" (the bot looked alive but /myfacts, the
            # behavior profiler, and /tldr were silently broken).
            for ddl in (
                "ALTER TABLE user_facts ADD COLUMN source_chat_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE user_facts ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private'",
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass  # column already exists
            # Create tables that may be brand-new anyway (no-op if present).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_facts (
                    user_id INTEGER NOT NULL,
                    fact TEXT NOT NULL,
                    source_chat_id TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, fact)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS behavior_stats (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    stat_date TEXT NOT NULL,
                    msg_count INTEGER DEFAULT 0,
                    total_chars INTEGER DEFAULT 0,
                    reply_count INTEGER DEFAULT 0,
                    link_count INTEGER DEFAULT 0,
                    question_count INTEGER DEFAULT 0,
                    media_count INTEGER DEFAULT 0,
                    sample_lines TEXT DEFAULT '',
                    last_hour INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id, stat_date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_log (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT DEFAULT '',
                    text TEXT NOT NULL,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_log_chat_ts
                ON chat_log(chat_id, ts)
            """)
            conn.commit()

    # ---- writes ----
    def save_message(self, chat_id, role, text):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO messages (chat_id, role, text) VALUES (?, ?, ?)",
                (str(chat_id), role, text),
            )
            conn.commit()

    def mark_bot_media_message(self, chat_id, message_id, kind="instagram"):
        """Record a message the bot sent that's just re-uploaded media, so
        should_skip_bot_media_reply() can recognize replies to it later."""
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO bot_media_messages (chat_id, message_id, kind) VALUES (?, ?, ?)",
                (str(chat_id), int(message_id), kind),
            )
            conn.commit()

    def is_bot_media_message(self, chat_id, message_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM bot_media_messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
            (str(chat_id), int(message_id)),
        ).fetchone()
        return row is not None

    def cleanup_old_bot_media_messages(self, max_age_days=60):
        """Bound table growth — nobody's realistically replying to a
        two-month-old Instagram download, and this table has no other GC."""
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM bot_media_messages WHERE created_at < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            conn.commit()

    def clear_chat(self, chat_id):
        """Wipes a chat's CONVERSATIONAL memory (messages + rolling summary).

        Deliberately does NOT touch user_facts: long-term facts and the
        behavioral profiles are durable knowledge about PEOPLE (how they
        talk, what they like), not about this chat's conversation — wiping
        them on /reset would erase personality knowledge the user wants
        kept. Users who want facts gone have explicit control: /forget all."""
        with self._write_lock:
            conn = self._connect()
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (str(chat_id),))
            conn.execute("DELETE FROM summaries WHERE chat_id = ?", (str(chat_id),))
            conn.commit()

    def delete_last_user_message(self, chat_id):
        """Remove the newest user row from a chat's history — used by /stop
        so an aborted (half-sent / accidental) turn doesn't linger in the
        session's memory. Returns the deleted text, or None if nothing was
        deleted."""
        with self._write_lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT id, text FROM messages WHERE chat_id = ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1",
                (str(chat_id),),
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM messages WHERE id = ?", (row[0],))
            conn.commit()
            return row[1]

    def save_summary(self, chat_id, summary, prune_ids):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """INSERT INTO summaries (chat_id, summary, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     summary=excluded.summary, updated_at=CURRENT_TIMESTAMP""",
                (str(chat_id), summary),
            )
            if prune_ids:
                qmarks = ",".join("?" * len(prune_ids))
                conn.execute(
                    f"DELETE FROM messages WHERE id IN ({qmarks})",
                    tuple(prune_ids),
                )
            conn.commit()

    # ---- reads ----
    def get_history(self, chat_id, limit=8):
        conn = self._connect()
        rows = conn.execute(
            """SELECT role, text FROM (
                   SELECT role, text, id FROM messages
                   WHERE chat_id = ? ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (str(chat_id), limit),
        ).fetchall()
        history = []
        for role, text in rows:
            api_role = "assistant" if role == "model" else role
            history.append({"role": api_role, "content": text})
        return history

    def get_summary(self, chat_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT summary FROM summaries WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return row[0] if row else ""

    def count_messages(self, chat_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return row[0] if row else 0

    def fetch_oldest_for_summary(self, chat_id, batch):
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, role, text FROM messages WHERE chat_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (str(chat_id), batch),
        ).fetchall()
        return rows

    # =========================
    # Todo CRUD
    # =========================
    def add_todo(self, chat_id, creator_id, text, priority, category, deadline=None,
                 recurrence="none", assignee_id=None, assignee_username=None):
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                """INSERT INTO todos
                   (chat_id, creator_id, text, priority, category, deadline,
                    recurrence, assignee_id, assignee_username)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(chat_id), int(creator_id), text, priority, category, deadline,
                 recurrence or "none", assignee_id, assignee_username),
            )
            conn.commit()
            return cur.lastrowid

    def get_active_todos(self, chat_id):
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, text, priority, category, creator_id, deadline, "
            "recurrence, assignee_id, assignee_username FROM todos "
            "WHERE chat_id = ? AND status = 0 ORDER BY id ASC",
            (str(chat_id),),
        ).fetchall()
        return rows

    def get_expired_todos(self, chat_id):
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, text, priority, category, creator_id, deadline, "
            "recurrence, assignee_id, assignee_username FROM todos "
            "WHERE chat_id = ? AND status = 2 ORDER BY id DESC LIMIT 10",
            (str(chat_id),),
        ).fetchall()
        return rows

    def mark_todo_done(self, todo_id):
        with self._write_lock:
            conn = self._connect()
            conn.execute("UPDATE todos SET status = 1 WHERE id = ?", (int(todo_id),))
            conn.commit()

    def mark_todo_expired(self, todo_id):
        with self._write_lock:
            conn = self._connect()
            conn.execute("UPDATE todos SET status = 2 WHERE id = ?", (int(todo_id),))
            conn.commit()

    def update_todo_deadline(self, todo_id, expected_deadline, new_deadline):
        """Conditional roll-forward for recurring-todo expiry alerts: only
        advances when the deadline still holds the value the dispatcher saw.
        Returns rows updated (0 = resolved concurrently — skip). Also used
        for plain deadline edits only through this guarded path."""
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE todos SET deadline = ?, attempts = 0, claimed_at = NULL, "
                "claim_token = NULL WHERE id = ? AND deadline = ? AND status = 0",
                (float(new_deadline), int(todo_id), float(expected_deadline)),
            )
            conn.commit()
            return cur.rowcount

    def claim_due_todos(self, now_epoch, token, stale_seconds=300):
        """Atomically claim due active todos for this dispatch batch.
        Returns [(id, deadline)] owned by `token`; concurrent batches and
        fresh claims are excluded, stale ones (>stale_seconds) refire."""
        now = float(now_epoch)
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE todos SET claimed_at = ?, claim_token = ? "
                "WHERE status = 0 AND deadline IS NOT NULL AND deadline <= ? "
                "AND (claimed_at IS NULL OR claimed_at < ?)",
                (now, str(token), now, now - int(stale_seconds)),
            )
            rows = conn.execute(
                "SELECT id, deadline FROM todos WHERE claim_token = ?",
                (str(token),),
            ).fetchall()
            conn.commit()
            return rows

    def release_todo_claim(self, todo_id):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE todos SET claimed_at = NULL, claim_token = NULL WHERE id = ?",
                (int(todo_id),),
            )
            conn.commit()

    def note_todo_failure(self, todo_id):
        """Record one permanent dispatch failure; releases the claim so the
        next cycle retries. Returns the new attempt count."""
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE todos SET attempts = attempts + 1, claimed_at = NULL, "
                "claim_token = NULL WHERE id = ?",
                (int(todo_id),),
            )
            row = conn.execute(
                "SELECT attempts FROM todos WHERE id = ?", (int(todo_id),)
            ).fetchone()
            conn.commit()
            return int(row[0]) if row else 0

    def delete_todo(self, todo_id):
        with self._write_lock:
            conn = self._connect()
            conn.execute("DELETE FROM todos WHERE id = ?", (int(todo_id),))
            conn.commit()

    def get_todo_by_id(self, todo_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT id, chat_id, creator_id, text, priority, category, status, "
            "deadline, recurrence, assignee_id, assignee_username "
            "FROM todos WHERE id = ?",
            (int(todo_id),),
        ).fetchone()
        return row

    # =========================
    # Reminder CRUD
    # =========================
    def add_reminder(self, chat_id, creator_id, text, mention_type, recurrence, trigger_time,
                      assignee_id=None, assignee_username=None):
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                """INSERT INTO reminders
                   (chat_id, creator_id, text, mention_type, recurrence, trigger_time,
                    assignee_id, assignee_username)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(chat_id), int(creator_id), text, mention_type, recurrence,
                 float(trigger_time), assignee_id, assignee_username),
            )
            conn.commit()
            return cur.lastrowid

    def snooze_reminder(self, reminder_id, new_trigger_time):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE reminders SET trigger_time = ?, claimed_at = NULL, "
                "claim_token = NULL WHERE id = ?",
                (float(new_trigger_time), int(reminder_id)),
            )
            conn.commit()

    def dismiss_reminder(self, reminder_id):
        with self._write_lock:
            conn = self._connect()
            conn.execute("DELETE FROM reminders WHERE id = ?", (int(reminder_id),))
            conn.commit()

    def reschedule_reminder(self, reminder_id, new_trigger_time):
        """Re-schedule a recurring reminder to a new future trigger_time, keeping status=0."""
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE reminders SET trigger_time = ?, status = 0 WHERE id = ?",
                (float(new_trigger_time), int(reminder_id)),
            )
            conn.commit()

    def reschedule_reminder_if_expected(self, reminder_id, expected_trigger, new_trigger_time):
        """Conditional dispatcher finalize: only advances when the trigger
        still holds the value the dispatcher saw (prevents double-advance on
        refire after a crash between send and finalize). Resets attempts and
        releases the claim. Returns rows updated (0 = resolved concurrently)."""
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE reminders SET trigger_time = ?, status = 0, attempts = 0, "
                "claimed_at = NULL, claim_token = NULL "
                "WHERE id = ? AND trigger_time = ? AND status = 0",
                (float(new_trigger_time), int(reminder_id), float(expected_trigger)),
            )
            conn.commit()
            return cur.rowcount

    def claim_due_reminders(self, now_epoch, token, stale_seconds=300):
        """Atomically claim due pending reminders for this dispatch batch.
        Returns [(id, trigger_time)] owned by `token`; concurrent batches
        and fresh claims are excluded, stale ones (>stale_seconds) refire."""
        now = float(now_epoch)
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE reminders SET claimed_at = ?, claim_token = ? "
                "WHERE status = 0 AND trigger_time <= ? "
                "AND (claimed_at IS NULL OR claimed_at < ?)",
                (now, str(token), now, now - int(stale_seconds)),
            )
            rows = conn.execute(
                "SELECT id, trigger_time FROM reminders WHERE claim_token = ?",
                (str(token),),
            ).fetchall()
            conn.commit()
            return rows

    def release_reminder_claim(self, reminder_id):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE reminders SET claimed_at = NULL, claim_token = NULL WHERE id = ?",
                (int(reminder_id),),
            )
            conn.commit()

    def note_reminder_failure(self, reminder_id):
        """Record one permanent dispatch failure; releases the claim so the
        next cycle retries. Returns the new attempt count."""
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE reminders SET attempts = attempts + 1, claimed_at = NULL, "
                "claim_token = NULL WHERE id = ?",
                (int(reminder_id),),
            )
            row = conn.execute(
                "SELECT attempts FROM reminders WHERE id = ?", (int(reminder_id),)
            ).fetchone()
            conn.commit()
            return int(row[0]) if row else 0

    def get_reminder_by_id(self, reminder_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT id, chat_id, creator_id, text, mention_type, recurrence, trigger_time, "
            "status, assignee_id, assignee_username "
            "FROM reminders WHERE id = ?",
            (int(reminder_id),),
        ).fetchone()
        return row

    # =========================
    # Feature 1: known_chats / group_members (real broadcast + mention data)
    # =========================
    def upsert_known_chat(self, chat_id, chat_type, username, title=None):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """INSERT INTO known_chats (chat_id, type, username, title, last_seen)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     type=excluded.type, username=excluded.username,
                     title=excluded.title, last_seen=CURRENT_TIMESTAMP""",
                (str(chat_id), chat_type, username, title),
            )
            conn.commit()

    def upsert_group_member(self, chat_id, user_id, username, first_name):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """INSERT INTO group_members (chat_id, user_id, username, first_name, last_seen)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name,
                     last_seen=CURRENT_TIMESTAMP""",
                (str(chat_id), int(user_id), username, first_name),
            )
            conn.commit()

    def get_known_chat_ids(self, types_):
        conn = self._connect()
        qmarks = ",".join("?" * len(types_))
        rows = conn.execute(
            f"SELECT chat_id FROM known_chats WHERE type IN ({qmarks})", tuple(types_)
        ).fetchall()
        out = []
        for (cid,) in rows:
            try:
                out.append(int(cid))
            except (TypeError, ValueError):
                continue
        return out

    def get_known_chats_with_titles(self, types_):
        """(chat_id, title, last_seen) rows for the admin's group list —
        title falls back to username or 'بدون عنوان' at display time."""
        conn = self._connect()
        qmarks = ",".join("?" * len(types_))
        return conn.execute(
            f"""SELECT chat_id, COALESCE(title, ''), COALESCE(username, ''), last_seen
                FROM known_chats WHERE type IN ({qmarks})
                ORDER BY last_seen DESC""",
            tuple(types_),
        ).fetchall()

    def get_group_members(self, chat_id):
        conn = self._connect()
        rows = conn.execute(
            "SELECT user_id, username, first_name FROM group_members "
            "WHERE chat_id = ? ORDER BY last_seen DESC",
            (str(chat_id),),
        ).fetchall()
        return rows

    def resolve_username_in_chat(self, chat_id, username):
        conn = self._connect()
        row = conn.execute(
            "SELECT user_id FROM group_members WHERE chat_id = ? AND username = ? COLLATE NOCASE",
            (str(chat_id), username),
        ).fetchone()
        return row[0] if row else None

    # =========================
    # Per-user sessions (private chats)
    # =========================
    def list_sessions(self, user_id, limit=20):
        """Newest-first session list for a user. Reads happen on the calling
        (worker) thread's connection — SQLite WAL lets concurrent reads run
        while writes serialize on the write lock."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT session_id, title, created_at FROM sessions "
            "WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
        return rows

    def count_sessions(self, user_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return row[0] if row else 0

    def create_session(self, user_id, session_id, title=""):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, user_id, title) VALUES (?, ?, ?)",
                (str(session_id), int(user_id), title),
            )
            conn.execute(
                "INSERT INTO user_active_sessions (user_id, active_session_id) "
                "VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "active_session_id=excluded.active_session_id",
                (int(user_id), str(session_id)),
            )
            conn.commit()

    def rename_session(self, user_id, session_id, title):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ? AND user_id = ?",
                (title, str(session_id), int(user_id)),
            )
            conn.commit()

    def delete_session(self, user_id, session_id):
        """Delete one session (its messages + rolling summary + registry row).
        Clears the active pointer when it pointed at the deleted session.
        Returns True if a session actually existed and was removed."""
        with self._write_lock:
            conn = self._connect()
            existed = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND user_id = ?",
                (str(session_id), int(user_id)),
            ).fetchone() is not None
            if not existed:
                return False
            memory_id = f"{int(user_id)}#{str(session_id)}"
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (memory_id,))
            conn.execute("DELETE FROM summaries WHERE chat_id = ?", (memory_id,))
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ? AND user_id = ?",
                (str(session_id), int(user_id)),
            )
            conn.execute(
                "UPDATE user_active_sessions SET active_session_id = NULL "
                "WHERE user_id = ? AND active_session_id = ?",
                (int(user_id), str(session_id)),
            )
            conn.commit()
            return True

    def sessions_has_title(self, user_id, session_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT title FROM sessions WHERE session_id = ? AND user_id = ?",
            (str(session_id), int(user_id)),
        ).fetchone()
        return bool(row and row[0])

    def session_exists(self, user_id, session_id):
        conn = self._connect()
        return conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ? AND user_id = ?",
            (str(session_id), int(user_id)),
        ).fetchone() is not None

    # ---- Digest memory (repeat-news guard) ----
    def save_digest_lines(self, digest_date, lines):
        """Record this run's digest bullet lines. `digest_date` is an ISO
        'YYYY-MM-DD' string (Iran local)."""
        if not lines:
            return
        with self._write_lock:
            conn = self._connect()
            conn.executemany(
                "INSERT INTO digest_history (digest_date, line) VALUES (?, ?)",
                [(str(digest_date), ln) for ln in lines],
            )
            # GC: keep only the last DIGEST_MEMORY_DAYS worth of rows.
            conn.execute(
                "DELETE FROM digest_history WHERE digest_date < date('now', ?)",
                (f"-{int(DIGEST_MEMORY_DAYS)} days",),
            )
            conn.commit()

    def get_recent_digest_lines(self, exclude_date=None):
        """All headline lines recorded in the last DIGEST_MEMORY_DAYS,
        oldest first, optionally excluding one day (so today's own lines
        don't leak into the prompt on a same-day manual re-run)."""
        conn = self._connect()
        if exclude_date:
            rows = conn.execute(
                "SELECT line FROM digest_history WHERE digest_date != ? ORDER BY id ASC",
                (str(exclude_date),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT line FROM digest_history ORDER BY id ASC"
            ).fetchall()
        return [r[0] for r in rows]

    # ---- Per-user long-term facts (portable memory, chat-scoped) ----
    def add_user_facts(self, user_id, facts, source_chat_id="", visibility="private",
                       max_per_user=50):
        """Insert facts (INSERT OR IGNORE — duplicates are idempotent), then
        trim the user's profile to the newest `max_per_user` facts. Each fact
        records where it was learned (source_chat_id + visibility) so
        injection can be scoped: private facts follow the user across their
        own private chats, group facts never leave that group. Returns
        how many were actually NEW (for metrics)."""
        added = 0
        with self._write_lock:
            conn = self._connect()
            for fact in facts:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO user_facts (user_id, fact, source_chat_id, visibility)
                       VALUES (?, ?, ?, ?)""",
                    (int(user_id), str(fact), str(source_chat_id), str(visibility)),
                )
                added += cur.rowcount
            conn.execute(
                "DELETE FROM user_facts WHERE user_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM user_facts WHERE user_id = ? "
                " ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (int(user_id), int(user_id), int(max_per_user)),
            )
            conn.commit()
        return added

    def get_user_facts(self, user_id, limit=None, source_chat_id=None,
                       include_private=True):
        """Facts for context injection, scoped by where the message is being
        answered:
          - private chat (source_chat_id=None): the user's private-learned
            facts (from any of their private chats — that's their own space).
          - group (source_chat_id=chat_id): that group's group-learned facts
            PLUS the user's private facts (their own, safe anywhere).
        `include_private=False` fetches ONLY group facts (used by /myfacts
        numbering in groups so /forget numbers match what's shown)."""
        conn = self._connect()
        params = [int(user_id)]
        conds = []
        if source_chat_id is not None:
            # Group context: group facts of this group + the user's private facts.
            conds.append(
                "((visibility = 'group' AND source_chat_id = ?) OR visibility = 'private')"
            )
            params.append(str(source_chat_id))
        else:
            # Private context: only private-learned facts.
            conds.append("visibility = 'private'")
        if not include_private:
            conds.append("visibility = 'group'")
        q = ("SELECT fact FROM user_facts WHERE user_id = ? AND "
             + " AND ".join(f"({c})" for c in conds)
             + " ORDER BY created_at ASC, rowid ASC")
        if limit:
            q += " LIMIT ?"
            params.append(int(limit))
        return [r[0] for r in conn.execute(q, params).fetchall()]

    def list_user_facts_scoped(self, user_id, source_chat_id=None):
        """Facts for /myfacts display, mirroring get_user_facts scoping so
        /forget <n> numbers always match what the user sees."""
        return self.get_user_facts(user_id, limit=None,
                                   source_chat_id=source_chat_id,
                                   include_private=True)

    def delete_user_fact(self, user_id, fact):
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM user_facts WHERE user_id = ? AND fact = ?",
                (int(user_id), str(fact)),
            )
            conn.commit()
            return cur.rowcount > 0

    # ---- Behavioral profile accumulators (zero-LLM-cost collection) ----
    def record_behavior(self, chat_id, user_id, msg_len, is_reply, has_link,
                        is_question, is_media, sample_line, hour):
        """Upsert today's per-(chat,user) behavior counters. One SQLite write
        per group message — cheap, no LLM involved. Sample lines are capped
        (6 lines x 80 chars) so the row can't grow unbounded."""
        try:
            with self._write_lock:
                conn = self._connect()
                conn.execute(
                    """INSERT INTO behavior_stats
                       (chat_id, user_id, stat_date, msg_count, total_chars,
                        reply_count, link_count, question_count, media_count,
                        sample_lines, last_hour)
                       VALUES (?, ?, date('now'), 1, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(chat_id, user_id, stat_date) DO UPDATE SET
                         msg_count = msg_count + 1,
                         total_chars = total_chars + excluded.total_chars,
                         reply_count = reply_count + excluded.reply_count,
                         link_count = link_count + excluded.link_count,
                         question_count = question_count + excluded.question_count,
                         media_count = media_count + excluded.media_count,
                         sample_lines = SUBSTR(
                           sample_lines || CHAR(10) || excluded.sample_lines, -480),
                         last_hour = excluded.last_hour""",
                    (str(chat_id), int(user_id), int(msg_len),
                     1 if is_reply else 0, 1 if has_link else 0,
                     1 if is_question else 0, 1 if is_media else 0,
                     str(sample_line or "")[:80], int(hour)),
                )
                conn.commit()
        except Exception:
            pass  # telemetry must never break the message pipeline

    def get_day_behavior_rows(self, stat_date=None):
        """All behavior rows for a given day (default: today UTC, matching
        the date('now') stamp written by record_behavior). Used by the
        nightly analyzer."""
        conn = self._connect()
        if stat_date is None:
            stat_date = conn.execute("SELECT date('now')").fetchone()[0]
        return conn.execute(
            """SELECT chat_id, user_id, msg_count, total_chars, reply_count,
                      link_count, question_count, media_count, sample_lines, last_hour
               FROM behavior_stats WHERE stat_date = ? AND msg_count >= 1
               ORDER BY msg_count DESC""",
            (stat_date,),
        ).fetchall()

    def purge_old_behavior_stats(self, keep_days=3):
        """Behavior rows are only needed until the nightly analyzer runs;
        keep a couple of days of slack, drop the rest."""
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM behavior_stats WHERE stat_date < date('now', ?)",
                (f"-{int(keep_days)} days",),
            )
            conn.commit()

    # ---- Group chat log (full-chatter buffer for profiler + /tldr) ----
    def log_group_chat(self, chat_id, user_id, first_name, text):
        """Append one group text message to the chat log. Best-effort."""
        try:
            with self._write_lock:
                conn = self._connect()
                conn.execute(
                    "INSERT INTO chat_log (chat_id, user_id, first_name, text) VALUES (?, ?, ?, ?)",
                    (str(chat_id), int(user_id), str(first_name or "")[:60], str(text)[:1000]),
                )
                conn.commit()
        except Exception:
            pass

    def get_chat_log_window(self, chat_id, hours=24, max_chars=20000, limit=800):
        """A group's chat log for the last `hours` hours, oldest-first
        (insert order via rowid), capped to the most recent `limit` rows."""
        conn = self._connect()
        return conn.execute(
            """SELECT user_id, first_name, text, ts FROM (
                   SELECT user_id, first_name, text, ts, rowid AS rid FROM chat_log
                   WHERE chat_id = ? AND ts > datetime('now', ?)
                   ORDER BY rowid DESC LIMIT ?
               ) ORDER BY rid ASC""",
            (str(chat_id), f"-{int(hours)} hours", int(limit)),
        ).fetchall()

    def purge_old_chat_log(self, keep_hours=48):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM chat_log WHERE ts < datetime('now', ?)",
                (f"-{int(keep_hours)} hours",),
            )
            conn.commit()

    def purge_old_messages(self, keep_days=90):
        """Delete conversational history older than keep_days. Summaries and
        user_facts are untouched (compressed/durable memory survives).
        Returns the number of rows deleted. Runs daily from the backup job."""
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM messages WHERE timestamp < datetime('now', ?)",
                (f"-{int(keep_days)} days",),
            )
            conn.commit()
            return cur.rowcount

    def get_maintenance_value(self, key):
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM maintenance_meta WHERE key = ?", (str(key),)
        ).fetchone()
        return row[0] if row else None

    def set_maintenance_value(self, key, value):
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO maintenance_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )
            conn.commit()

    def maybe_vacuum(self, interval_days=7):
        """Rebuild the DB file to reclaim space from purged rows, at most
        once per interval_days (restart-safe via maintenance_meta). Returns
        True when a VACUUM actually ran. Uses a throwaway connection so it
        never interferes with the pooled thread-local ones."""
        import time as _time

        try:
            last = float(self.get_maintenance_value("last_vacuum") or 0)
        except (TypeError, ValueError):
            last = 0
        if _time.time() - last < int(interval_days) * 86400:
            return False
        with self._write_lock:
            conn = sqlite3.connect(self.db_file, timeout=60)
            try:
                conn.execute("VACUUM")
                conn.commit()
            finally:
                conn.close()
        self.set_maintenance_value("last_vacuum", _time.time())
        return True

    def checkpoint_wal(self):
        """Checkpoint WAL frames into the main DB file (TRUNCATE mode resets
        the WAL afterwards). Called once during graceful shutdown so a
        restart starts clean with nothing to replay. Returns the
        (busy, log_frames, checkpointed_frames) triple. Uses a throwaway
        connection so pooled thread-local ones are untouched."""
        with self._write_lock:
            conn = sqlite3.connect(self.db_file, timeout=60)
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                conn.commit()
            finally:
                conn.close()
        return tuple(row) if row else (0, 0, 0)

    def delete_all_user_facts(self, user_id):
        """Wipes ALL of a user's facts (private + every group) — used by
        /forget all, which is an explicit privacy action by the user."""
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM user_facts WHERE user_id = ?", (int(user_id),)
            )
            conn.commit()
            return cur.rowcount

    def get_active_session(self, user_id):
        """Returns the user's active session_id, or None (no active session —
        plain user_id memory) or 'deleted' (pointer referenced a session that
        was already removed; treated the same as None by callers)."""
        conn = self._connect()
        row = conn.execute(
            "SELECT active_session_id FROM user_active_sessions WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if not row or row[0] is None:
            return None
        exists = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ? AND user_id = ?",
            (str(row[0]), int(user_id)),
        ).fetchone()
        if not exists:
            return "deleted"
        return row[0]

    def set_active_session(self, user_id, session_id):
        """Activate an existing session. No-op (returns False) if the session
        doesn't belong to this user — that's the ownership check."""
        with self._write_lock:
            conn = self._connect()
            exists = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND user_id = ?",
                (str(session_id), int(user_id)),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "INSERT INTO user_active_sessions (user_id, active_session_id) "
                "VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "active_session_id=excluded.active_session_id",
                (int(user_id), str(session_id)),
            )
            conn.commit()
            return True


repo = ChatRepository(DB_FILE)


def fetch_all_group_ids() -> list:
    """Real broadcast targets — every group/supergroup this bot has seen a
    message from, sourced from the known_chats table."""
    return repo.get_known_chat_ids(("group", "supergroup"))

def fetch_all_user_ids() -> list:
    """Real broadcast targets — every private-chat user this bot has seen a
    message from, sourced from the known_chats table."""
    return repo.get_known_chat_ids(("private",))


def _build_all_members_mention(chat_id_str, limit=25):
    """Best-effort 'tag everyone' workaround: the Bot API has no native way
    to ping an entire group, so we tag every member this bot has personally
    seen post in this chat (tracked via group_members). Members who never
    spoke since the bot joined can't be included — this is a hard platform
    limitation, not a bug."""
    members = repo.get_group_members(chat_id_str)
    if not members:
        return "@all"
    tags = []
    for user_id, username, first_name in members[:limit]:
        name = first_name or (f"@{username}" if username else str(user_id))
        tags.append(f"[{name}](tg://user?id={user_id})")
    text = " ".join(tags)
    if len(members) > limit:
        text += f" +{len(members) - limit} نفر دیگر"
    return text
