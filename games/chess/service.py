import asyncio
import json
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from games.chess.clock import ChessClock
from games.chess.engine import ChessEngine
from games.chess.models import (
    GameEndReason,
    GameResult,
    TimeControl,
    TIME_CONTROLS,
)
from realtime.protocol import (
    make_clock_sync,
    make_game_finished,
    make_game_start,
    make_game_state,
    make_sync,
)
from repositories.games import GameRepository

log = logging.getLogger("L-bot")


@dataclass
class ChessRoom:
    room_id: str
    status: str  # "waiting" | "active" | "finished" | "abandoned"
    player1_id: int
    player1_name: str
    player2_id: Optional[int] = None
    player2_name: Optional[str] = None
    white_id: Optional[int] = None
    black_id: Optional[int] = None
    engine: ChessEngine = field(default_factory=ChessEngine)
    clock: Optional[ChessClock] = None
    time_control_key: str = "blitz_5_0"
    time_control: TimeControl = field(default_factory=lambda: TimeControl.from_key("blitz_5_0"))
    current_seq: int = 0
    invite_code: str = ""
    draw_offered_by: Optional[int] = None
    move_history: List[Dict[str, Any]] = field(default_factory=list)
    seen_move_ids: set = field(default_factory=set)
    last_move: Optional[str] = None
    result: Optional[str] = None
    end_reason: Optional[str] = None
    turn_started_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class ChessService:
    """Authoritative orchestrator for chess game lifecycle, rules verification,
    move execution, clock updates, and SQLite WAL state persistence.
    """

    def __init__(self, repository: GameRepository, connection_manager):
        self.repo = repository
        self.manager = connection_manager
        self._rooms: Dict[str, ChessRoom] = {}
        self._global_lock = asyncio.Lock()

    def get_room(self, room_id: str) -> Optional[ChessRoom]:
        return self._rooms.get(room_id)

    def get_rooms_for_player(self, user_id: int) -> List[str]:
        return [
            r.room_id
            for r in self._rooms.values()
            if r.status in ("waiting", "active") and (r.player1_id == user_id or r.player2_id == user_id)
        ]

    async def create_room(
        self,
        creator_id: int,
        creator_name: str,
        time_control_key: str = "blitz_5_0",
    ) -> Dict[str, Any]:
        """Create a private game room with an unguessable ID and invitation code."""
        room_id = secrets.token_urlsafe(12)
        invite_code = secrets.token_urlsafe(8)
        tc = TimeControl.from_key(time_control_key)
        clock = ChessClock(tc.initial_ms, tc.initial_ms, tc.increment_ms)

        room = ChessRoom(
            room_id=room_id,
            status="waiting",
            player1_id=creator_id,
            player1_name=creator_name,
            time_control_key=time_control_key,
            time_control=tc,
            clock=clock,
            invite_code=invite_code,
            created_at=time.time(),
            updated_at=time.time(),
        )

        async with self._global_lock:
            self._rooms[room_id] = room

        # Persist room to database
        self.repo.create_room(
            room_id=room_id,
            player1_id=creator_id,
            initial_time_ms=tc.initial_ms,
            increment_ms=tc.increment_ms,
            invite_code=invite_code,
            state_json=json.dumps({"fen": room.engine.get_fen(), "status": "waiting"}),
            expires_at=time.time() + 3600 * 24,  # 24h expiration for waiting rooms
        )

        log.info("Chess room created: %s by user %d (%s) [TC: %s]", room_id, creator_id, creator_name, time_control_key)
        return {
            "room_id": room_id,
            "invite_code": invite_code,
            "time_control": {"key": tc.key, "label": tc.label, "initial_ms": tc.initial_ms, "increment_ms": tc.increment_ms},
        }

    async def join_room(self, room_id: str, player_id: int, player_name: str) -> Dict[str, Any]:
        """Join a waiting room as Player 2. Starts the match and assigns colors."""
        room = self.get_room(room_id)
        if not room:
            # Check DB in case room was not in memory
            db_room = self.repo.get_room(room_id)
            if not db_room:
                return {"error": "room_not_found", "message": "اتاق پیدا نشد."}
            await self._restore_room_from_dict(db_room)
            room = self.get_room(room_id)

        if not room:
            return {"error": "room_not_found", "message": "اتاق پیدا نشد."}

        async with room.lock:
            if room.status != "waiting":
                if player_id in (room.player1_id, room.player2_id):
                    # Player re-joining active/waiting game
                    return {"room_id": room_id, "status": room.status}
                return {"error": "room_not_waiting", "message": "بازی قبلاً شروع شده یا پایان یافته است."}

            if room.player1_id == player_id:
                return {"error": "cannot_join_own_room", "message": "نمی‌توانید به بازی خودتان متصل شوید."}

            # Assign White and Black fairly at random
            colors = [room.player1_id, player_id]
            random.shuffle(colors)
            white_id, black_id = colors[0], colors[1]

            room.player2_id = player_id
            room.player2_name = player_name
            room.white_id = white_id
            room.black_id = black_id
            room.status = "active"
            room.turn_started_at = time.time()
            room.updated_at = time.time()

            # Start clock on White's turn
            if room.clock:
                room.clock.start("white")

            # Persist update in SQLite
            state_dict = {
                "fen": room.engine.get_fen(),
                "turn": room.engine.get_turn(),
                "player1_name": room.player1_name,
                "player2_name": room.player2_name,
            }
            self.repo.join_room(
                room_id=room_id,
                player2_id=player_id,
                white_player_id=white_id,
                black_player_id=black_id,
                status="active",
                state_json=json.dumps(state_dict),
                turn_started_at=room.turn_started_at,
            )

            p1_info = {"user_id": room.player1_id, "name": room.player1_name}
            p2_info = {"user_id": player_id, "name": player_name}
            white_info = p1_info if white_id == room.player1_id else p2_info
            black_info = p2_info if white_id == room.player1_id else p1_info

            start_msg = make_game_start(
                game_id=room_id,
                white_info=white_info,
                black_info=black_info,
                fen=room.engine.get_fen(),
                time_control={
                    "key": room.time_control.key,
                    "label": room.time_control.label,
                    "initial_ms": room.time_control.initial_ms,
                    "increment_ms": room.time_control.increment_ms,
                },
                seq=room.current_seq,
            )

            log.info("Chess room %s started: White=%d, Black=%d", room_id, white_id, black_id)
            return {"room_id": room_id, "start_message": start_msg}

    async def join_by_invite(self, invite_code: str, player_id: int, player_name: str) -> Dict[str, Any]:
        """Lookup room by invitation code and join."""
        # Find in memory
        target_room_id = None
        for r in self._rooms.values():
            if r.invite_code == invite_code:
                target_room_id = r.room_id
                break

        if not target_room_id:
            db_room = self.repo.get_room_by_invite(invite_code)
            if not db_room:
                return {"error": "invalid_invite", "message": "لینک دعوت معتبر نیست یا منقضی شده است."}
            target_room_id = db_room["id"]

        return await self.join_room(target_room_id, player_id, player_name)

    async def make_move(
        self,
        room_id: str,
        player_id: int,
        uci: str,
        move_id: str,
        expected_seq: int,
    ) -> Dict[str, Any]:
        """Validate, execute, and persist an authoritative chess move."""
        room = self.get_room(room_id)
        if not room:
            return {"error": "room_not_found", "message": "اتاق پیدا نشد."}

        async with room.lock:
            if room.status != "active":
                return {"error": "game_not_active", "message": "بازی در حال حاضر فعال نیست."}

            # Sequence check for stale or out-of-order updates
            if expected_seq != room.current_seq:
                return {
                    "error": "stale_sequence",
                    "message": f"شماره توالی نامعتبر است (انتظار: {room.current_seq}, دریافتی: {expected_seq}).",
                }

            # Idempotency check: reject already executed move_id
            if move_id in room.seen_move_ids:
                return {"error": "duplicate_move", "message": "این حرکت قبلاً ثبت شده است."}

            # Turn and role authorization
            current_turn = room.engine.get_turn()
            expected_player = room.white_id if current_turn == "white" else room.black_id
            if player_id != expected_player:
                return {"error": "not_your_turn", "message": "نوبت شما نیست!"}

            # Clock check: has time already expired?
            if room.clock:
                flag = room.clock.is_flag_fallen()
                if flag:
                    return await self._handle_timeout(room, flag)

            # Move validation and execution on authoritative engine
            if not room.engine.is_legal_move(uci):
                return {"error": "illegal_move", "message": "حرکت نامعتبر یا غیرقانونی است."}

            try:
                san, fen_after = room.engine.make_move(uci)
            except ValueError as e:
                return {"error": "illegal_move", "message": str(e)}

            # Update clock
            if room.clock:
                room.clock.stop_and_switch()

            # Clear any pending draw offer on move
            room.draw_offered_by = None

            # Check terminal conditions
            term_result = room.engine.get_result()
            finished = False
            finished_message = None
            new_turn = room.engine.get_turn()

            if term_result is not None:
                finished = True
                res, reason = term_result
                room.status = "finished"
                room.result = res.value
                room.end_reason = reason.value

                winner_id = None
                if res == GameResult.WHITE_WINS:
                    winner_id = room.white_id
                    self.repo.update_stats(room.white_id, "chess", "win")
                    self.repo.update_stats(room.black_id, "chess", "loss")
                elif res == GameResult.BLACK_WINS:
                    winner_id = room.black_id
                    self.repo.update_stats(room.black_id, "chess", "win")
                    self.repo.update_stats(room.white_id, "chess", "loss")
                else:
                    self.repo.update_stats(room.white_id, "chess", "draw")
                    self.repo.update_stats(room.black_id, "chess", "draw")

                self.repo.set_room_result(room.room_id, f"{res.value}:{reason.value}")
                finished_message = make_game_finished(
                    game_id=room_id,
                    result=res.value,
                    reason=reason.value,
                    winner_id=winner_id,
                    seq=room.current_seq + 1,
                )
            else:
                # Game continues: start clock for opponent
                if room.clock:
                    room.clock.start(new_turn)

            # Advance sequence
            room.current_seq += 1
            room.seen_move_ids.add(move_id)
            room.last_move = uci
            room.turn_started_at = time.time()
            room.updated_at = time.time()

            w_ms, b_ms = room.clock.get_remaining() if room.clock else (0, 0)
            move_entry = {
                "seq": room.current_seq,
                "uci": uci,
                "san": san,
                "player_id": player_id,
            }
            room.move_history.append(move_entry)

            # Persist move and updated state to database in atomic step
            self.repo.save_move(
                room_id=room_id,
                seq=room.current_seq,
                player_id=player_id,
                move_id=move_id,
                action_json=json.dumps({"uci": uci, "san": san}),
                uci=uci,
                san=san,
                fen_after=fen_after,
                time_remaining_ms=w_ms if current_turn == "white" else b_ms,
            )

            state_snapshot = {
                "fen": fen_after,
                "turn": new_turn,
                "check": room.engine.is_check(),
                "last_move": uci,
                "status": room.status,
            }
            self.repo.update_room_state(
                room_id=room_id,
                state_json=json.dumps(state_snapshot),
                current_seq=room.current_seq,
                white_remaining_ms=w_ms,
                black_remaining_ms=b_ms,
                turn_started_at=room.turn_started_at,
                status=room.status if finished else None,
                result=f"{room.result}:{room.end_reason}" if finished else None,
            )

            state_msg = make_game_state(
                game_id=room_id,
                seq=room.current_seq,
                fen=fen_after,
                turn=new_turn,
                last_move=uci,
                check=room.engine.is_check(),
                status=room.status,
                white_ms=w_ms,
                black_ms=b_ms,
                move_history=room.move_history,
            )

            return {
                "state_message": state_msg,
                "finished": finished,
                "finished_message": finished_message,
            }

    async def resign(self, room_id: str, player_id: int) -> Dict[str, Any]:
        """Handle player resignation."""
        room = self.get_room(room_id)
        if not room:
            return {"error": "room_not_found", "message": "اتاق پیدا نشد."}

        async with room.lock:
            if room.status != "active":
                return {"error": "game_not_active", "message": "بازی فعال نیست."}

            if player_id not in (room.white_id, room.black_id):
                return {"error": "unauthorized", "message": "شما بازیکن این بازی نیستید."}

            if room.clock:
                room.clock.pause()

            if player_id == room.white_id:
                winner_id = room.black_id
                res = GameResult.BLACK_WINS
            else:
                winner_id = room.white_id
                res = GameResult.WHITE_WINS

            room.status = "finished"
            room.result = res.value
            room.end_reason = GameEndReason.RESIGNATION.value
            room.current_seq += 1
            room.updated_at = time.time()

            # Record stats
            if winner_id:
                self.repo.update_stats(winner_id, "chess", "win")
            self.repo.update_stats(player_id, "chess", "loss")

            self.repo.set_room_result(room_id, f"{res.value}:resignation")

            w_ms, b_ms = room.clock.get_remaining() if room.clock else (0, 0)
            state_msg = make_game_state(
                game_id=room_id,
                seq=room.current_seq,
                fen=room.engine.get_fen(),
                turn=room.engine.get_turn(),
                last_move=room.last_move,
                check=room.engine.is_check(),
                status=room.status,
                white_ms=w_ms,
                black_ms=b_ms,
                move_history=room.move_history,
            )

            finished_msg = make_game_finished(
                game_id=room_id,
                result=res.value,
                reason="resignation",
                winner_id=winner_id,
                seq=room.current_seq,
            )

            return {
                "state_message": state_msg,
                "finished_message": finished_msg,
            }

    async def offer_draw(self, room_id: str, player_id: int) -> Dict[str, Any]:
        """Offer draw to opponent."""
        room = self.get_room(room_id)
        if not room or room.status != "active":
            return {"error": "game_not_active", "message": "بازی فعال نیست."}

        async with room.lock:
            if player_id not in (room.white_id, room.black_id):
                return {"error": "unauthorized", "message": "شما بازیکن این مسابقه نیستید."}
            room.draw_offered_by = player_id
            return {"ok": True}

    async def accept_draw(self, room_id: str, player_id: int) -> Dict[str, Any]:
        """Accept an incoming draw offer."""
        room = self.get_room(room_id)
        if not room or room.status != "active":
            return {"error": "game_not_active", "message": "بازی فعال نیست."}

        async with room.lock:
            if not room.draw_offered_by:
                return {"error": "no_draw_offer", "message": "پیشنهاد تساوی فعالی وجود ندارد."}
            if room.draw_offered_by == player_id:
                return {"error": "cannot_accept_own_draw", "message": "نمی‌توانید پیشنهاد تساوی خود را قبول کنید."}

            if room.clock:
                room.clock.pause()

            room.status = "finished"
            room.result = GameResult.DRAW.value
            room.end_reason = GameEndReason.DRAW_AGREEMENT.value
            room.draw_offered_by = None
            room.current_seq += 1
            room.updated_at = time.time()

            if room.white_id and room.black_id:
                self.repo.update_stats(room.white_id, "chess", "draw")
                self.repo.update_stats(room.black_id, "chess", "draw")

            self.repo.set_room_result(room_id, "draw:draw_agreement")

            w_ms, b_ms = room.clock.get_remaining() if room.clock else (0, 0)
            state_msg = make_game_state(
                game_id=room_id,
                seq=room.current_seq,
                fen=room.engine.get_fen(),
                turn=room.engine.get_turn(),
                last_move=room.last_move,
                check=room.engine.is_check(),
                status=room.status,
                white_ms=w_ms,
                black_ms=b_ms,
                move_history=room.move_history,
            )

            finished_msg = make_game_finished(
                game_id=room_id,
                result="draw",
                reason="draw_agreement",
                winner_id=None,
                seq=room.current_seq,
            )

            return {"state_message": state_msg, "finished_message": finished_msg}

    async def decline_draw(self, room_id: str, player_id: int) -> Dict[str, Any]:
        """Decline incoming draw offer."""
        room = self.get_room(room_id)
        if not room:
            return {"error": "room_not_found"}

        async with room.lock:
            if room.draw_offered_by and room.draw_offered_by != player_id:
                room.draw_offered_by = None
                return {"ok": True}
            return {"error": "no_active_offer"}

    async def get_sync_state(self, room_id: str) -> Optional[Dict[str, Any]]:
        """Return full authoritative snapshot for client resynchronization / reconnection."""
        room = self.get_room(room_id)
        if not room:
            db_room = self.repo.get_room(room_id)
            if db_room:
                await self._restore_room_from_dict(db_room)
                room = self.get_room(room_id)

        if not room:
            return None

        async with room.lock:
            w_ms, b_ms = room.clock.get_remaining() if room.clock else (0, 0)
            return make_sync(
                game_id=room_id,
                seq=room.current_seq,
                full_state={
                    "status": room.status,
                    "fen": room.engine.get_fen(),
                    "turn": room.engine.get_turn(),
                    "check": room.engine.is_check(),
                    "last_move": room.last_move,
                    "white_ms": w_ms,
                    "black_ms": b_ms,
                    "white_id": room.white_id,
                    "black_id": room.black_id,
                    "player1_name": room.player1_name,
                    "player2_name": room.player2_name,
                    "time_control": {
                        "key": room.time_control.key,
                        "label": room.time_control.label,
                    },
                    "move_history": room.move_history,
                    "result": room.result,
                    "end_reason": room.end_reason,
                    "draw_offered_by": room.draw_offered_by,
                },
            )

    async def check_timeouts(self) -> List[Dict[str, Any]]:
        """Check all active rooms for clock flag-falls. Called periodically."""
        results = []
        for room in list(self._rooms.values()):
            if room.status != "active" or not room.clock:
                continue
            flag = room.clock.is_flag_fallen()
            if flag:
                async with room.lock:
                    if room.status == "active":
                        res = await self._handle_timeout(room, flag)
                        if res.get("finished"):
                            results.append(
                                {
                                    "room_id": room.room_id,
                                    "state_message": res["state_message"],
                                    "finished_message": res["finished_message"],
                                }
                            )
        return results

    async def _handle_timeout(self, room: ChessRoom, flag_color: str) -> Dict[str, Any]:
        """Resolve game upon clock expiration."""
        if room.clock:
            room.clock.pause()

        # Flag fall: if White timed out, Black wins (unless Black has insufficient material for checkmate)
        winner_id = None
        if flag_color == "white":
            # Opponent is Black
            if room.engine.is_insufficient_material():
                res = GameResult.DRAW
            else:
                res = GameResult.BLACK_WINS
                winner_id = room.black_id
        else:
            # Opponent is White
            if room.engine.is_insufficient_material():
                res = GameResult.DRAW
            else:
                res = GameResult.WHITE_WINS
                winner_id = room.white_id

        room.status = "finished"
        room.result = res.value
        room.end_reason = GameEndReason.TIMEOUT.value
        room.current_seq += 1
        room.updated_at = time.time()

        # Update stats
        if res == GameResult.DRAW:
            if room.white_id and room.black_id:
                self.repo.update_stats(room.white_id, "chess", "draw")
                self.repo.update_stats(room.black_id, "chess", "draw")
        elif winner_id:
            loser_id = room.black_id if winner_id == room.white_id else room.white_id
            self.repo.update_stats(winner_id, "chess", "win")
            if loser_id:
                self.repo.update_stats(loser_id, "chess", "loss")

        self.repo.set_room_result(room.room_id, f"{res.value}:timeout")

        w_ms, b_ms = room.clock.get_remaining() if room.clock else (0, 0)
        state_msg = make_game_state(
            game_id=room.room_id,
            seq=room.current_seq,
            fen=room.engine.get_fen(),
            turn=room.engine.get_turn(),
            last_move=room.last_move,
            check=room.engine.is_check(),
            status=room.status,
            white_ms=w_ms,
            black_ms=b_ms,
            move_history=room.move_history,
        )

        finished_msg = make_game_finished(
            game_id=room.room_id,
            result=res.value,
            reason="timeout",
            winner_id=winner_id,
            seq=room.current_seq,
        )

        log.info("Room %s ended on timeout by %s. Result: %s", room.room_id, flag_color, res.value)
        return {
            "finished": True,
            "state_message": state_msg,
            "finished_message": finished_msg,
        }

    async def recover_from_db(self) -> int:
        """On process startup, restore all active/waiting rooms from SQLite."""
        active_rooms = self.repo.get_all_active_rooms()
        count = 0
        for r_dict in active_rooms:
            try:
                await self._restore_room_from_dict(r_dict)
                count += 1
            except Exception:
                log.error("Failed to restore room %s", r_dict.get("id"), exc_info=True)
        log.info("Recovered %d rooms from SQLite into memory", count)
        return count

    async def _restore_room_from_dict(self, r: Dict[str, Any]) -> None:
        room_id = r["id"]
        tc = TimeControl(
            key="custom",
            initial_ms=int(r["initial_time_ms"]),
            increment_ms=int(r["increment_ms"]),
        )
        clock = ChessClock(
            white_ms=int(r["white_remaining_ms"]),
            black_ms=int(r["black_remaining_ms"]),
            increment_ms=int(r["increment_ms"]),
        )

        # Restore moves
        moves = self.repo.get_moves(room_id)
        engine = ChessEngine()
        move_history = []
        seen_move_ids = set()
        last_move = None

        for m in moves:
            uci = m.get("uci") or ""
            if uci:
                try:
                    san, _ = engine.make_move(uci)
                    last_move = uci
                except Exception:
                    pass
            seen_move_ids.add(m["move_id"])
            move_history.append(
                {
                    "seq": m["seq"],
                    "uci": uci,
                    "san": m.get("san") or "",
                    "player_id": m["player_id"],
                }
            )

        # Restore clock active state if currently running
        if r["status"] == "active":
            active_color = engine.get_turn()
            clock.restore(
                white_ms=int(r["white_remaining_ms"]),
                black_ms=int(r["black_remaining_ms"]),
                active_color=active_color,
                turn_started_at=r.get("turn_started_at"),
            )

        room = ChessRoom(
            room_id=room_id,
            status=r["status"],
            player1_id=r["player1_id"],
            player1_name=f"Player {r['player1_id']}",
            player2_id=r.get("player2_id"),
            player2_name=f"Player {r.get('player2_id')}" if r.get("player2_id") else None,
            white_id=r.get("white_player_id"),
            black_id=r.get("black_player_id"),
            engine=engine,
            clock=clock,
            time_control=tc,
            current_seq=int(r.get("current_seq") or 0),
            invite_code=r.get("invite_code") or "",
            move_history=move_history,
            seen_move_ids=seen_move_ids,
            last_move=last_move,
            turn_started_at=r.get("turn_started_at"),
            created_at=float(r.get("created_at") or time.time()),
            updated_at=float(r.get("updated_at") or time.time()),
        )

        self._rooms[room_id] = room
