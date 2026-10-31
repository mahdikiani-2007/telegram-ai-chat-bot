import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import DB_FILE

log = logging.getLogger("L-bot")


class GameRepository:
    """Thread-safe SQLite repository for game state persistence.
    Follows existing WAL mode and thread-local connection pool pattern from db.py.
    """

    def __init__(self, db_file: Optional[str] = None):
        self.db_file = db_file or DB_FILE
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_file, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._write_lock:
            conn = self._connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_rooms (
                    id TEXT PRIMARY KEY,
                    game_type TEXT NOT NULL DEFAULT 'chess',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    player1_id INTEGER NOT NULL,
                    player2_id INTEGER,
                    white_player_id INTEGER,
                    black_player_id INTEGER,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    current_seq INTEGER NOT NULL DEFAULT 0,
                    initial_time_ms INTEGER NOT NULL,
                    increment_ms INTEGER NOT NULL DEFAULT 0,
                    white_remaining_ms INTEGER NOT NULL,
                    black_remaining_ms INTEGER NOT NULL,
                    turn_started_at REAL,
                    invite_code TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL,
                    result TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_rooms_status ON game_rooms(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_rooms_invite ON game_rooms(invite_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_rooms_players ON game_rooms(player1_id, player2_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    move_id TEXT NOT NULL,
                    action_json TEXT NOT NULL DEFAULT '{}',
                    uci TEXT NOT NULL DEFAULT '',
                    san TEXT NOT NULL DEFAULT '',
                    fen_after TEXT NOT NULL DEFAULT '',
                    time_remaining_ms INTEGER,
                    created_at REAL NOT NULL,
                    UNIQUE(room_id, seq),
                    UNIQUE(room_id, move_id),
                    FOREIGN KEY(room_id) REFERENCES game_rooms(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_moves_room_seq ON game_moves(room_id, seq)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_stats (
                    user_id INTEGER NOT NULL,
                    game_type TEXT NOT NULL DEFAULT 'chess',
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    draws INTEGER NOT NULL DEFAULT 0,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    kot_wins INTEGER NOT NULL DEFAULT 0,
                    hakem_kot_wins INTEGER NOT NULL DEFAULT 0,
                    win_streak INTEGER NOT NULL DEFAULT 0,
                    longest_win_streak INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, game_type)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_players (
                    room_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    seat INTEGER NOT NULL,
                    team INTEGER,
                    player_type TEXT NOT NULL DEFAULT 'human',
                    ai_difficulty TEXT,
                    connection_status TEXT NOT NULL DEFAULT 'connected',
                    joined_at REAL NOT NULL,
                    PRIMARY KEY(room_id, seat)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_players_user ON game_players(user_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    actor_id INTEGER,
                    event_type TEXT NOT NULL,
                    event_payload TEXT NOT NULL DEFAULT '{}',
                    request_id TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(room_id, seq)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_events_room_seq ON game_events(room_id, seq)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_ratings (
                    user_id INTEGER NOT NULL,
                    game_type TEXT NOT NULL,
                    rating INTEGER NOT NULL DEFAULT 1000,
                    tier TEXT NOT NULL DEFAULT 'Bronze',
                    matches_played INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, game_type)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_game_ratings_type_rating ON game_ratings(game_type, rating DESC)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS match_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    game_type TEXT NOT NULL,
                    winner_id INTEGER,
                    winner_team INTEGER,
                    final_score TEXT NOT NULL,
                    hands_played INTEGER NOT NULL DEFAULT 1,
                    kot_count INTEGER NOT NULL DEFAULT 0,
                    hakem_kot_count INTEGER NOT NULL DEFAULT 0,
                    match_duration_seconds REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_match_history_game_type ON match_history(game_type, completed_at DESC)")
            conn.commit()

    def create_room(
        self,
        room_id: str,
        player1_id: int,
        initial_time_ms: int,
        increment_ms: int,
        invite_code: str,
        state_json: str,
        expires_at: Optional[float] = None,
        game_type: str = "chess",
    ) -> None:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO game_rooms (
                    id, game_type, status, player1_id, state_json, current_seq,
                    initial_time_ms, increment_ms, white_remaining_ms, black_remaining_ms,
                    invite_code, created_at, updated_at, expires_at
                ) VALUES (?, ?, 'waiting', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    game_type,
                    player1_id,
                    state_json,
                    initial_time_ms,
                    increment_ms,
                    initial_time_ms,
                    initial_time_ms,
                    invite_code,
                    now,
                    now,
                    expires_at,
                ),
            )
            conn.commit()

    def get_room(self, room_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute("SELECT * FROM game_rooms WHERE id = ?", (room_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_room_by_invite(self, invite_code: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute("SELECT * FROM game_rooms WHERE invite_code = ?", (invite_code,))
        row = cur.fetchone()
        return dict(row) if row else None

    def join_room(
        self,
        room_id: str,
        player2_id: int,
        white_player_id: int,
        black_player_id: int,
        status: str = "active",
        state_json: Optional[str] = None,
        turn_started_at: Optional[float] = None,
    ) -> bool:
        now = time.time()
        turn_start = turn_started_at or now
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                """
                UPDATE game_rooms SET
                    player2_id = ?,
                    white_player_id = ?,
                    black_player_id = ?,
                    status = ?,
                    state_json = COALESCE(?, state_json),
                    turn_started_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'waiting'
                """,
                (
                    player2_id,
                    white_player_id,
                    black_player_id,
                    status,
                    state_json,
                    turn_start,
                    now,
                    room_id,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_room_state(
        self,
        room_id: str,
        state_json: str,
        current_seq: int,
        white_remaining_ms: int,
        black_remaining_ms: int,
        turn_started_at: Optional[float],
        status: Optional[str] = None,
        result: Optional[str] = None,
    ) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            if status is not None:
                cur = conn.execute(
                    """
                    UPDATE game_rooms SET
                        state_json = ?,
                        current_seq = ?,
                        white_remaining_ms = ?,
                        black_remaining_ms = ?,
                        turn_started_at = ?,
                        status = ?,
                        result = COALESCE(?, result),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        state_json,
                        current_seq,
                        white_remaining_ms,
                        black_remaining_ms,
                        turn_started_at,
                        status,
                        result,
                        now,
                        room_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE game_rooms SET
                        state_json = ?,
                        current_seq = ?,
                        white_remaining_ms = ?,
                        black_remaining_ms = ?,
                        turn_started_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        state_json,
                        current_seq,
                        white_remaining_ms,
                        black_remaining_ms,
                        turn_started_at,
                        now,
                        room_id,
                    ),
                )
            conn.commit()
            return cur.rowcount > 0

    def save_move(
        self,
        room_id: str,
        seq: int,
        player_id: int,
        move_id: str,
        action_json: str,
        uci: str = "",
        san: str = "",
        fen_after: str = "",
        time_remaining_ms: Optional[int] = None,
    ) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO game_moves (
                        room_id, seq, player_id, move_id, action_json,
                        uci, san, fen_after, time_remaining_ms, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_id,
                        seq,
                        player_id,
                        move_id,
                        action_json,
                        uci,
                        san,
                        fen_after,
                        time_remaining_ms,
                        now,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                log.warning("Duplicate move rejected for room %s seq %d move_id %s", room_id, seq, move_id)
                return False

    def get_moves(self, room_id: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT * FROM game_moves
            WHERE room_id = ?
            ORDER BY seq ASC
            """,
            (room_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_active_rooms_for_player(self, player_id: int) -> List[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT * FROM game_rooms
            WHERE (player1_id = ? OR player2_id = ?)
              AND status IN ('waiting', 'active')
            ORDER BY updated_at DESC
            """,
            (player_id, player_id),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_all_active_rooms(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT * FROM game_rooms
            WHERE status IN ('waiting', 'active')
            ORDER BY created_at ASC
            """
        )
        return [dict(r) for r in cur.fetchall()]

    def set_room_result(self, room_id: str, result: str, status: str = "finished") -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                """
                UPDATE game_rooms SET
                    status = ?,
                    result = ?,
                    updated_at = ?
                WHERE id = ? AND status != 'finished'
                """,
                (status, result, now, room_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_stats(self, user_id: int, game_type: str, outcome: str) -> None:
        """Atomically upsert stats: outcome in ('win', 'loss', 'draw')."""
        w = 1 if outcome == "win" else 0
        l = 1 if outcome == "loss" else 0
        d = 1 if outcome == "draw" else 0
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO game_stats (user_id, game_type, wins, losses, draws, games_played)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id, game_type) DO UPDATE SET
                    wins = wins + excluded.wins,
                    losses = losses + excluded.losses,
                    draws = draws + excluded.draws,
                    games_played = games_played + 1
                """,
                (user_id, game_type, w, l, d),
            )
            conn.commit()

    def get_stats(self, user_id: int, game_type: str = "chess") -> Dict[str, int]:
        conn = self._connect()
        cur = conn.execute(
            "SELECT wins, losses, draws, games_played FROM game_stats WHERE user_id = ? AND game_type = ?",
            (user_id, game_type),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
        return {"wins": 0, "losses": 0, "draws": 0, "games_played": 0}

    def get_leaderboard(self, game_type: str = "chess", limit: int = 20) -> List[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT user_id, wins, losses, draws, games_played
            FROM game_stats
            WHERE game_type = ?
            ORDER BY wins DESC, games_played DESC
            LIMIT ?
            """,
            (game_type, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def cleanup_expired_rooms(self, now: Optional[float] = None) -> int:
        cutoff = now or time.time()
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                """
                UPDATE game_rooms
                SET status = 'abandoned', updated_at = ?
                WHERE status = 'waiting' AND expires_at IS NOT NULL AND expires_at < ?
                """,
                (cutoff, cutoff),
            )
            conn.commit()
            return cur.rowcount

    # =========================================================================
    # Hokm & Extended Card Game Methods
    # =========================================================================

    @staticmethod
    def rating_to_tier(rating: int) -> str:
        if rating < 1100:
            return "Bronze"
        elif rating < 1300:
            return "Silver"
        elif rating < 1500:
            return "Gold"
        elif rating < 1700:
            return "Platinum"
        elif rating < 1900:
            return "Diamond"
        return "Master"

    def get_or_create_rating(self, user_id: int, game_type: str) -> Dict[str, Any]:
        conn = self._connect()
        cur = conn.execute(
            "SELECT * FROM game_ratings WHERE user_id = ? AND game_type = ?",
            (user_id, game_type),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

        now = time.time()
        with self._write_lock:
            conn.execute(
                """
                INSERT OR IGNORE INTO game_ratings (
                    user_id, game_type, rating, tier, matches_played, wins, losses, updated_at
                ) VALUES (?, ?, 1000, 'Bronze', 0, 0, 0, ?)
                """,
                (user_id, game_type, now),
            )
            conn.commit()
        return {
            "user_id": user_id,
            "game_type": game_type,
            "rating": 1000,
            "tier": "Bronze",
            "matches_played": 0,
            "wins": 0,
            "losses": 0,
            "updated_at": now,
        }

    def update_rating_match(
        self,
        winner_ids: List[int],
        loser_ids: List[int],
        game_type: str,
        is_draw: bool = False,
    ) -> Dict[int, Dict[str, Any]]:
        """Updates ratings for players using standard Elo calculation.
        For teams, uses average rating of team to calculate expected score.
        """
        now = time.time()
        results: Dict[int, Dict[str, Any]] = {}

        # Fetch current ratings
        winner_ratings = [self.get_or_create_rating(uid, game_type)["rating"] for uid in winner_ids]
        loser_ratings = [self.get_or_create_rating(uid, game_type)["rating"] for uid in loser_ids]

        avg_winner = sum(winner_ratings) / max(1, len(winner_ratings))
        avg_loser = sum(loser_ratings) / max(1, len(loser_ratings))

        # Expected score
        ea = 1.0 / (1.0 + 10.0 ** ((avg_loser - avg_winner) / 400.0))
        eb = 1.0 - ea

        sa = 0.5 if is_draw else 1.0
        sb = 0.5 if is_draw else 0.0

        k = 32
        delta_winner = int(round(k * (sa - ea)))
        delta_loser = int(round(k * (sb - eb)))

        with self._write_lock:
            conn = self._connect()
            # Update winners
            for uid, r in zip(winner_ids, winner_ratings):
                new_r = max(100, r + delta_winner)
                tier = self.rating_to_tier(new_r)
                win_inc = 0 if is_draw else 1
                conn.execute(
                    """
                    INSERT INTO game_ratings (user_id, game_type, rating, tier, matches_played, wins, losses, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, 0, ?)
                    ON CONFLICT(user_id, game_type) DO UPDATE SET
                        rating = excluded.rating,
                        tier = excluded.tier,
                        matches_played = game_ratings.matches_played + 1,
                        wins = game_ratings.wins + excluded.wins,
                        updated_at = excluded.updated_at
                    """,
                    (uid, game_type, new_r, tier, win_inc, now),
                )
                results[uid] = {"old_rating": r, "new_rating": new_r, "delta": delta_winner, "tier": tier}

            # Update losers
            for uid, r in zip(loser_ids, loser_ratings):
                new_r = max(100, r + delta_loser)
                tier = self.rating_to_tier(new_r)
                loss_inc = 0 if is_draw else 1
                conn.execute(
                    """
                    INSERT INTO game_ratings (user_id, game_type, rating, tier, matches_played, wins, losses, updated_at)
                    VALUES (?, ?, ?, ?, 1, 0, ?, ?)
                    ON CONFLICT(user_id, game_type) DO UPDATE SET
                        rating = excluded.rating,
                        tier = excluded.tier,
                        matches_played = game_ratings.matches_played + 1,
                        losses = game_ratings.losses + excluded.losses,
                        updated_at = excluded.updated_at
                    """,
                    (uid, game_type, new_r, tier, loss_inc, now),
                )
                results[uid] = {"old_rating": r, "new_rating": new_r, "delta": delta_loser, "tier": tier}

            conn.commit()

        return results

    def save_hokm_room(
        self,
        room_id: str,
        game_type: str,
        host_id: int,
        invite_code: str,
        state_json: str,
        players: List[Dict[str, Any]],
        initial_time_ms: int = 30000,
        expires_at: Optional[float] = None,
    ) -> None:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO game_rooms (
                    id, game_type, status, player1_id, state_json, current_seq,
                    initial_time_ms, increment_ms, white_remaining_ms, black_remaining_ms,
                    invite_code, created_at, updated_at, expires_at
                ) VALUES (?, ?, 'waiting', ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    game_type,
                    host_id,
                    state_json,
                    initial_time_ms,
                    initial_time_ms,
                    initial_time_ms,
                    invite_code,
                    now,
                    now,
                    expires_at or (now + 86400),
                ),
            )
            # Sync players
            for p in players:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO game_players (
                        room_id, user_id, seat, team, player_type, ai_difficulty, connection_status, joined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_id,
                        p["user_id"],
                        p["seat"],
                        p.get("team"),
                        p.get("player_type", "human"),
                        p.get("ai_difficulty"),
                        p.get("connection_status", "connected"),
                        now,
                    ),
                )
            conn.commit()

    def replace_hokm_room_players(
        self,
        room_id: str,
        players: List[Dict[str, Any]],
    ) -> None:
        """Atomically replace the persisted Hokm seat list for a room."""
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute("DELETE FROM game_players WHERE room_id = ?", (room_id,))
            for player in players:
                conn.execute(
                    """
                    INSERT INTO game_players (
                        room_id, user_id, seat, team, player_type, ai_difficulty,
                        connection_status, joined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_id,
                        int(player["user_id"]),
                        int(player["seat"]),
                        player.get("team"),
                        "ai" if player.get("is_ai") else "human",
                        player.get("ai_difficulty"),
                        "connected" if player.get("is_connected", True) else "disconnected",
                        now,
                    ),
                )
            conn.commit()

    def update_hokm_room_state(
        self,
        room_id: str,
        state_json: str,
        seq: int,
        status: str = "active",
        result: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE game_rooms
                SET state_json = ?, current_seq = ?, status = ?, result = COALESCE(?, result), updated_at = ?
                WHERE id = ?
                """,
                (state_json, seq, status, result, now, room_id),
            )
            conn.commit()

    def record_game_event(
        self,
        room_id: str,
        seq: int,
        actor_id: Optional[int],
        event_type: str,
        payload: Dict[str, Any],
        request_id: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR IGNORE INTO game_events (
                    room_id, seq, actor_id, event_type, event_payload, request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (room_id, seq, actor_id, event_type, json.dumps(payload), request_id, now),
            )
            conn.commit()

    def record_match_history(
        self,
        room_id: str,
        game_type: str,
        winner_id: Optional[int],
        winner_team: Optional[int],
        final_score: str,
        hands_played: int = 1,
        kot_count: int = 0,
        hakem_kot_count: int = 0,
        match_duration_seconds: float = 0.0,
    ) -> None:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO match_history (
                    room_id, game_type, winner_id, winner_team, final_score,
                    hands_played, kot_count, hakem_kot_count, match_duration_seconds, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    game_type,
                    winner_id,
                    winner_team,
                    final_score,
                    hands_played,
                    kot_count,
                    hakem_kot_count,
                    match_duration_seconds,
                    now,
                ),
            )
            conn.commit()

    def get_active_hokm_rooms(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT id, game_type, status, player1_id, state_json, current_seq, invite_code, created_at
            FROM game_rooms
            WHERE game_type LIKE 'hokm%' AND status IN ('waiting', 'active')
            ORDER BY created_at DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]

    def get_user_ratings_all_modes(self, user_id: int) -> Dict[str, Any]:
        conn = self._connect()
        cur = conn.execute(
            "SELECT game_type, rating, tier, matches_played, wins, losses FROM game_ratings WHERE user_id = ?",
            (user_id,),
        )
        return {r["game_type"]: dict(r) for r in cur.fetchall()}


game_repo = GameRepository()
