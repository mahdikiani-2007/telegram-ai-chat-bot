import asyncio
from dataclasses import asdict
from enum import Enum
import json
import logging
import secrets
import string
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from games.core.room import PlayerInfo
from games.hokm.ai.strategy import choose_draft_card_ai, choose_trump_ai, get_ai_action_decision
from games.hokm.four_player.engine import FourPlayerHokmEngine
from games.hokm.shared.cards import Card
from games.hokm.shared.models import (
    GameMode, HandSummary, HokmGameState, HokmStage, PlayedCard, PlayerSeat, Trick,
)
from games.hokm.three_player.engine import ThreePlayerHokmEngine
from games.hokm.two_player.engine import TwoPlayerHokmEngine
from realtime.manager import ConnectionManager
from repositories.games import GameRepository

log = logging.getLogger("L-bot")

EngineType = Union[TwoPlayerHokmEngine, ThreePlayerHokmEngine, FourPlayerHokmEngine]


class HokmRoom:
    """Represents an active in-memory Hokm room."""

    def __init__(
        self,
        room_id: str,
        game_mode: GameMode,
        host_id: int,
        invite_code: str,
        turn_time_limit: int = 30,
        is_ranked: bool = False,
        allow_spectators: bool = True,
    ):
        self.room_id = room_id
        self.game_mode = game_mode
        self.host_id = host_id
        self.invite_code = invite_code
        self.turn_time_limit = turn_time_limit
        self.is_ranked = is_ranked
        self.allow_spectators = allow_spectators
        self.lock = asyncio.Lock()
        self.seq = 0
        self.created_at = time.time()
        self.updated_at = time.time()
        self.spectators: set[int] = set()
        self.rematch_votes: set[int] = set()
        self.rematch_room_id: Optional[str] = None
        self.ai_task_generation = 0

        # Instantiate engine
        if game_mode == GameMode.TWO_PLAYER:
            self.engine: EngineType = TwoPlayerHokmEngine(room_id, turn_time_limit)
        elif game_mode == GameMode.THREE_PLAYER:
            self.engine: EngineType = ThreePlayerHokmEngine(room_id, turn_time_limit)
        else:
            self.engine: EngineType = FourPlayerHokmEngine(room_id, turn_time_limit)

        # Pending seats before match start
        self.seat_slots: List[Optional[Dict[str, Any]]] = [None] * game_mode.player_count


class HokmService:
    """Server-authoritative coordinator for all Hokm multiplayer matches."""

    def __init__(self, repo: GameRepository, manager: ConnectionManager):
        self.repo = repo
        self.manager = manager
        self.rooms: Dict[str, HokmRoom] = {}
        self.code_to_room: Dict[str, str] = {}
        self._user_to_room: Dict[int, str] = {}
        self._timeout_tasks: Dict[str, asyncio.Task] = {}

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    def _snapshot_room(self, room: HokmRoom) -> Dict[str, Any]:
        """Build a private, versioned snapshot for durable server-side recovery."""
        return {
            "schema_version": 1,
            "room": {
                "room_id": room.room_id,
                "game_mode": room.game_mode.value,
                "host_id": room.host_id,
                "invite_code": room.invite_code,
                "turn_time_limit": room.turn_time_limit,
                "is_ranked": room.is_ranked,
                "allow_spectators": room.allow_spectators,
                "seq": room.seq,
                "created_at": room.created_at,
                "updated_at": room.updated_at,
                "seat_slots": room.seat_slots,
                "spectators": sorted(room.spectators),
                "rematch_votes": sorted(room.rematch_votes),
                "rematch_room_id": room.rematch_room_id,
            },
            "engine": {
                "state": asdict(room.engine.state),
                "players": [asdict(player) for player in room.engine.players],
                "last_hand_summary": (
                    asdict(room.engine.last_hand_summary)
                    if room.engine.last_hand_summary is not None
                    else None
                ),
            },
        }

    def _persist_room(self, room: HokmRoom) -> None:
        """Persist complete engine state and seat metadata after each transition."""
        room.updated_at = time.time()
        stage = room.engine.state.stage
        status = (
            "waiting" if stage == HokmStage.WAITING
            else "completed" if stage == HokmStage.MATCH_ENDED
            else "active"
        )
        snapshot = self._snapshot_room(room)
        result = None
        if status == "completed":
            result = (
                f"winner_team:{room.engine.state.match_winner_team}"
                if room.engine.state.match_winner_team is not None
                else f"winner_seat:{room.engine.state.match_winner_seat}"
            )

        self.repo.update_hokm_room_state(
            room_id=room.room_id,
            state_json=json.dumps(
                snapshot,
                ensure_ascii=False,
                default=self._json_default,
            ),
            seq=room.seq,
            status=status,
            result=result,
        )

        source_players = (
            [
                {
                    "seat": index,
                    **slot,
                    "is_connected": True,
                }
                for index, slot in enumerate(room.seat_slots)
                if slot is not None
            ]
            if not room.engine.players
            else [asdict(player) for player in room.engine.players]
        )
        self.repo.replace_hokm_room_players(room.room_id, source_players)

    async def recover_from_db(self) -> int:
        """Restore waiting and active Hokm rooms, including private player hands."""
        restored = 0
        for row in self.repo.get_active_hokm_rooms():
            try:
                data = json.loads(row.get("state_json") or "{}")
                if data.get("schema_version") != 1:
                    log.warning(
                        "Skipping legacy Hokm room %s: no recoverable private snapshot",
                        row.get("id"),
                    )
                    continue

                metadata = data["room"]
                saved_engine = data["engine"]
                mode = GameMode(metadata["game_mode"])
                room = HokmRoom(
                    room_id=metadata["room_id"],
                    game_mode=mode,
                    host_id=int(metadata["host_id"]),
                    invite_code=metadata["invite_code"],
                    turn_time_limit=int(metadata["turn_time_limit"]),
                    is_ranked=bool(metadata["is_ranked"]),
                    allow_spectators=bool(metadata["allow_spectators"]),
                )
                room.seq = int(metadata.get("seq", row.get("current_seq") or 0))
                room.created_at = float(metadata.get("created_at", row.get("created_at") or time.time()))
                room.updated_at = float(metadata.get("updated_at", row.get("updated_at") or time.time()))
                room.seat_slots = metadata.get("seat_slots") or [None] * mode.player_count
                room.spectators = set(metadata.get("spectators") or [])
                room.rematch_votes = set(metadata.get("rematch_votes") or [])
                room.rematch_room_id = metadata.get("rematch_room_id")

                state_data = dict(saved_engine["state"])
                state_data["game_mode"] = GameMode(state_data["game_mode"])
                state_data["stage"] = HokmStage(state_data["stage"])
                state_data["current_trick"] = [
                    PlayedCard(**played) for played in state_data.get("current_trick", [])
                ]
                state_data["completed_tricks"] = [
                    Trick(
                        lead_seat=trick["lead_seat"],
                        lead_card=trick["lead_card"],
                        cards=[PlayedCard(**played) for played in trick.get("cards", [])],
                        winner_seat=trick.get("winner_seat"),
                        winner_id=trick.get("winner_id"),
                    )
                    for trick in state_data.get("completed_tricks", [])
                ]
                state_data["ceremony_cards"] = {
                    int(seat): card for seat, card in state_data.get("ceremony_cards", {}).items()
                }
                room.engine.state = HokmGameState(**state_data)
                room.engine.players = [
                    PlayerSeat(**player) for player in saved_engine.get("players", [])
                ]

                summary = saved_engine.get("last_hand_summary")
                if summary:
                    summary = dict(summary)
                    summary["tricks_by_seat"] = {
                        int(seat): count for seat, count in summary.get("tricks_by_seat", {}).items()
                    }
                    room.engine.last_hand_summary = HandSummary(**summary)

                self.rooms[room.room_id] = room
                if room.invite_code:
                    self.code_to_room[room.invite_code] = room.room_id
                for slot in room.seat_slots:
                    if slot and not slot.get("is_ai"):
                        self._user_to_room[int(slot["user_id"])] = room.room_id
                for player in room.engine.players:
                    if not player.is_ai:
                        self._user_to_room[player.user_id] = room.room_id
                for spectator_id in room.spectators:
                    self._user_to_room[spectator_id] = room.room_id
                restored += 1
            except Exception:
                log.error("Failed to recover Hokm room %s", row.get("id"), exc_info=True)

        log.info("Recovered %d Hokm room(s) from SQLite", restored)
        return restored

    async def leave_room(self, room_id: str, user_id: int) -> Tuple[bool, str]:
        """Leave a waiting room, completed room, or spectator seat safely."""
        room = self.rooms.get(room_id)
        if room is None:
            return False, "اتاق پیدا نشد."

        async with room.lock:
            stage = room.engine.state.stage
            slot_index = next(
                (
                    index for index, slot in enumerate(room.seat_slots)
                    if slot and int(slot.get("user_id", 0)) == user_id
                ),
                None,
            )
            engine_seat = next(
                (
                    player.seat for player in room.engine.players
                    if player.user_id == user_id and not player.is_ai
                ),
                None,
            )

            if stage not in (HokmStage.WAITING, HokmStage.MATCH_ENDED):
                if engine_seat is not None:
                    return False, "برای خروج از بازی فعال ابتدا باید بازی تمام شود."

            if stage == HokmStage.WAITING and user_id == room.host_id:
                for slot in room.seat_slots:
                    if slot and not slot.get("is_ai"):
                        self._user_to_room.pop(int(slot["user_id"]), None)
                        await self.manager.leave_room(int(slot["user_id"]), room_id)
                for spectator_id in tuple(room.spectators):
                    self._user_to_room.pop(spectator_id, None)
                    await self.manager.leave_room(spectator_id, room_id)
                room.spectators.clear()
                room.engine.state.stage = HokmStage.MATCH_ENDED
                room.updated_at = time.time()
                self.code_to_room.pop(room.invite_code, None)
                self._user_to_room.pop(user_id, None)
                self.repo.update_hokm_room_state(
                    room_id=room_id,
                    state_json=json.dumps(self._snapshot_room(room), default=self._json_default),
                    seq=room.seq,
                    status="abandoned",
                    result="abandoned",
                )
                return True, "اتاق انتظار لغو شد."

            was_spectator = user_id in room.spectators
            if slot_index is not None and stage == HokmStage.WAITING:
                room.seat_slots[slot_index] = None
            if stage == HokmStage.MATCH_ENDED and engine_seat is not None:
                player = room.engine.players[engine_seat]
                player.is_connected = False
                player.disconnect_time = time.time()
            room.spectators.discard(user_id)
            room.rematch_votes.discard(user_id)
            self._user_to_room.pop(user_id, None)
            await self.manager.leave_room(user_id, room_id)

            if stage in (HokmStage.WAITING, HokmStage.MATCH_ENDED) or was_spectator:
                room.seq += 1
                self._persist_room(room)
                await self._broadcast_room_state(room)
            return True, "از اتاق خارج شدید."

    async def request_rematch(
        self,
        room_id: str,
        user_id: int,
    ) -> Tuple[bool, str, Optional[HokmRoom]]:
        """Require every human player to consent before restarting the same match."""
        room = self.rooms.get(room_id)
        if room is None:
            return False, "اتاق پیدا نشد.", None

        async with room.lock:
            if room.engine.state.stage != HokmStage.MATCH_ENDED:
                return False, "مسابقه هنوز تمام نشده است.", None

            seated_humans = {
                player.user_id for player in room.engine.players if not player.is_ai
            }
            connected_humans = seated_humans.intersection(
                self.manager.get_room_members(room.room_id)
            )
            if user_id not in connected_humans:
                return False, "برای درخواست بازی مجدد باید در اتاق مسابقه متصل باشید.", None
            human_ids = connected_humans

            if user_id in room.rematch_votes:
                return False, f"درخواست شما قبلاً ثبت شده است ({len(room.rematch_votes)}/{len(human_ids)}).", None

            if room.rematch_room_id:
                rematch = self.rooms.get(room.rematch_room_id)
                return (
                    (True, "بازی مجدد قبلاً ساخته شده است.", rematch)
                    if rematch else (False, "اتاق بازی مجدد پیدا نشد.", None)
                )

            room.rematch_votes.add(user_id)
            room.seq += 1
            self._persist_room(room)
            await self._broadcast_room_state(room)

            if not human_ids.issubset(room.rematch_votes):
                return False, f"درخواست ثبت شد ({len(room.rematch_votes)}/{len(human_ids)}). منتظر موافقت سایر بازیکنان.", None

            old_players = []
            for player in room.engine.players:
                player_data = asdict(player)
                if not player.is_ai and player.user_id not in human_ids:
                    # A human who left the finished room is replaced by a
                    # medium bot in the rematch rather than being silently
                    # enrolled without consent.
                    player_data.update({
                        "user_id": -(2000 + player.seat),
                        "name": "هوش مصنوعی (متوسط)",
                        "is_ai": True,
                        "ai_difficulty": "medium",
                        "cards": [],
                        "is_connected": True,
                        "disconnect_time": None,
                    })
                old_players.append(player_data)
            old_room_id = room.room_id
            mode = room.game_mode.value
            old_host_id = room.host_id
            time_limit = room.turn_time_limit
            ranked = room.is_ranked
            allow_spectators = room.allow_spectators
            invite_code = self._generate_code()

            new_room = HokmRoom(
                room_id=f"hokm_{secrets.token_hex(8)}",
                game_mode=room.game_mode,
                host_id=old_host_id if old_host_id in human_ids else user_id,
                invite_code=invite_code,
                turn_time_limit=time_limit,
                is_ranked=ranked and not any(player["is_ai"] for player in old_players),
                allow_spectators=allow_spectators,
            )
            new_room.seat_slots = [
                {
                    "user_id": player["user_id"],
                    "name": player["name"],
                    "team": player.get("team"),
                    "is_ai": player.get("is_ai", False),
                    "ai_difficulty": player.get("ai_difficulty"),
                    "is_connected": player.get("is_connected", True),
                }
                for player in old_players
            ]
            self.rooms[new_room.room_id] = new_room
            self.code_to_room[new_room.invite_code] = new_room.room_id
            for player in old_players:
                if not player.get("is_ai"):
                    self._user_to_room[int(player["user_id"])] = new_room.room_id
                    await self.manager.leave_room(int(player["user_id"]), old_room_id)
                    await self.manager.join_room(int(player["user_id"]), new_room.room_id)

            self.repo.save_hokm_room(
                room_id=new_room.room_id,
                game_type=mode,
                host_id=new_room.host_id,
                invite_code=new_room.invite_code,
                state_json=json.dumps(new_room.engine.get_public_state()),
                players=[
                    {**slot, "seat": index}
                    for index, slot in enumerate(new_room.seat_slots)
                ],
                initial_time_ms=time_limit * 1000,
            )
            self._persist_room(new_room)
            room.rematch_room_id = new_room.room_id
            room.seq += 1
            self._persist_room(room)

        started, message = await self.start_game(new_room.room_id, new_room.host_id)
        if not started:
            return False, message, None
        await self._broadcast_room_state(new_room)
        return True, "بازی مجدد شروع شد.", new_room

    def _generate_code(self) -> str:
        chars = string.ascii_uppercase + string.digits
        return "".join(secrets.choice(chars) for _ in range(6))

    async def create_room(
        self,
        host_id: int,
        host_name: str,
        mode_str: str = "hokm_4p",
        turn_time_limit: int = 30,
        is_ranked: bool = False,
        allow_spectators: bool = True,
    ) -> HokmRoom:
        """Creates a new Hokm room, releasing only old waiting/completed rooms."""
        existing_room_id = self._user_to_room.get(host_id)
        if existing_room_id:
            existing_room = self.rooms.get(existing_room_id)
            if existing_room:
                if existing_room.engine.state.stage not in (HokmStage.WAITING, HokmStage.MATCH_ENDED):
                    raise ValueError("شما در یک بازی فعال دیگر حضور دارید.")
                await self.leave_room(existing_room_id, host_id)

        mode = GameMode(mode_str)
        room_id = f"hokm_{secrets.token_hex(8)}"
        invite_code = self._generate_code()

        room = HokmRoom(
            room_id=room_id,
            game_mode=mode,
            host_id=host_id,
            invite_code=invite_code,
            turn_time_limit=turn_time_limit,
            is_ranked=is_ranked,
            allow_spectators=allow_spectators,
        )

        # Place host in Seat 0
        room.seat_slots[0] = {
            "user_id": host_id,
            "name": host_name,
            "team": 0 if mode == GameMode.FOUR_PLAYER else None,
            "is_ai": False,
            "ai_difficulty": None,
        }

        self.rooms[room_id] = room
        self.code_to_room[invite_code] = room_id
        self._user_to_room[host_id] = room_id

        # Persist initial room
        self.repo.save_hokm_room(
            room_id=room_id,
            game_type=mode.value,
            host_id=host_id,
            invite_code=invite_code,
            state_json=json.dumps(room.engine.get_public_state()),
            players=[{"user_id": host_id, "seat": 0, "team": 0 if mode == GameMode.FOUR_PLAYER else None}],
            initial_time_ms=turn_time_limit * 1000,
        )
        self._persist_room(room)

        return room

    async def join_room(self, user_id: int, user_name: str, room_id_or_code: str) -> Tuple[bool, str, Optional[HokmRoom]]:
        """Joins an existing room without stealing a player from another active match."""
        room_id = self.code_to_room.get(room_id_or_code.upper()) or room_id_or_code
        existing_room_id = self._user_to_room.get(user_id)
        if existing_room_id and existing_room_id != room_id:
            existing_room = self.rooms.get(existing_room_id)
            if existing_room:
                if existing_room.engine.state.stage not in (HokmStage.WAITING, HokmStage.MATCH_ENDED):
                    return False, "شما در یک بازی فعال دیگر حضور دارید.", None
                await self.leave_room(existing_room_id, user_id)

        room = self.rooms.get(room_id)
        if not room:
            return False, "اتاق پیدا نشد.", None

        async with room.lock:
            # Check if user already has a seat (reconnection)
            for idx, slot in enumerate(room.seat_slots):
                if slot and slot["user_id"] == user_id:
                    self._user_to_room[user_id] = room_id
                    # If game is already active, mark player reconnected
                    if room.engine.state.stage != HokmStage.WAITING:
                        room.engine.players[idx].is_connected = True
                        room.engine.players[idx].disconnect_time = None
                    else:
                        slot["is_connected"] = True
                    self._persist_room(room)
                    return True, "بازگشت به بازی.", room

            # If match already started: allow spectator or reject
            if room.engine.state.stage != HokmStage.WAITING:
                if room.allow_spectators:
                    room.spectators.add(user_id)
                    self._user_to_room[user_id] = room_id
                    self._persist_room(room)
                    return True, "ورود به عنوان تماشاگر.", room
                return False, "بازی قبلاً شروع شده است.", None

            # Find empty seat
            empty_seat = None
            for idx, slot in enumerate(room.seat_slots):
                if slot is None:
                    empty_seat = idx
                    break

            if empty_seat is None:
                if room.allow_spectators:
                    room.spectators.add(user_id)
                    self._user_to_room[user_id] = room_id
                    self._persist_room(room)
                    return True, "ظرفیت بازیکنان تکمیل است (ورود به عنوان تماشاگر).", room
                return False, "ظرفیت اتاق تکمیل است.", None

            # Assign seat
            team = (0 if empty_seat in (0, 2) else 1) if room.game_mode == GameMode.FOUR_PLAYER else None
            room.seat_slots[empty_seat] = {
                "user_id": user_id,
                "name": user_name,
                "team": team,
                "is_ai": False,
                "ai_difficulty": None,
            }
            self._user_to_room[user_id] = room_id
            self._persist_room(room)
            return True, "با موفقیت وارد شدید.", room

    async def add_ai_player(self, room_id: str, host_id: int, difficulty: str = "medium") -> Tuple[bool, str]:
        """Allows room host to add an AI player to an open seat."""
        room = self.rooms.get(room_id)
        if not room:
            return False, "اتاق پیدا نشد."
        if room.host_id != host_id:
            return False, "فقط سازنده اتاق می‌تواند ربات اضافه کند."
        if difficulty not in {"easy", "medium", "hard"}:
            return False, "سطح دشواری ربات نامعتبر است."
        if room.engine.state.stage != HokmStage.WAITING:
            return False, "بازی در حال اجراست."

        async with room.lock:
            if room.engine.state.stage != HokmStage.WAITING:
                return False, "بازی در حال اجراست."
            empty_seat = None
            for idx, slot in enumerate(room.seat_slots):
                if slot is None:
                    empty_seat = idx
                    break

            if empty_seat is None:
                return False, "جای خالی وجود ندارد."

            ai_id = -(1000 + empty_seat)
            diff_label = {"easy": "آسان", "medium": "متوسط", "hard": "حرفه‌ای"}.get(difficulty, "متوسط")
            ai_name = f"هوش مصنوعی ({diff_label})"
            team = (0 if empty_seat in (0, 2) else 1) if room.game_mode == GameMode.FOUR_PLAYER else None

            room.seat_slots[empty_seat] = {
                "user_id": ai_id,
                "name": ai_name,
                "team": team,
                "is_ai": True,
                "ai_difficulty": difficulty,
            }

            # AI games become unranked
            room.is_ranked = False
            self._persist_room(room)

            await self._broadcast_room_state(room)
            return True, "ربات اضافه شد."

    async def start_game(self, room_id: str, host_id: int) -> Tuple[bool, str]:
        """Starts the game when all required seats are occupied."""
        room = self.rooms.get(room_id)
        if not room:
            return False, "اتاق پیدا نشد."
        if room.host_id != host_id:
            return False, "فقط سازنده اتاق می‌تواند بازی را آغاز کند."

        async with room.lock:
            if room.engine.state.stage != HokmStage.WAITING:
                return False, "این اتاق قبلاً شروع شده یا دیگر در وضعیت انتظار نیست."
            if any(slot is None for slot in room.seat_slots):
                return False, "همه جایگاه‌ها باید پر شوند."

            # Initialize engine with players
            room.engine.initialize_players(room.seat_slots)

            # Stage 1: Card drawing ceremony to select Hakem
            room.engine.start_ceremony()
            room.seq += 1

            self._persist_room(room)
            await self._broadcast_room_state(room)

            # Wait 2.5s for ceremony animation, then deal first 5 cards
            asyncio.create_task(self._delay_and_start_first_hand(room_id))
            return True, "بازی با موفقیت شروع شد."

    async def _delay_and_start_first_hand(self, room_id: str) -> None:
        await asyncio.sleep(2.5)
        room = self.rooms.get(room_id)
        if not room:
            return
        async with room.lock:
            if room.engine.state.stage != HokmStage.CEREMONY:
                return
            room.engine.start_new_hand()
            room.seq += 1
            self._persist_room(room)
            await self._broadcast_room_state(room)
            # If Hakem is AI, let AI select trump
            await self._check_ai_turn(room)

    async def handle_trump_selection(self, room_id: str, user_id: int, suit_str: str) -> Tuple[bool, str]:
        """Processes Hakem's trump selection."""
        room = self.rooms.get(room_id)
        if not room:
            return False, "اتاق پیدا نشد."

        async with room.lock:
            hakem_seat = room.engine.state.hakem_seat
            if hakem_seat is None or room.engine.players[hakem_seat].user_id != user_id:
                return False, "فقط حاکم می‌تواند حکم کند."

            success = room.engine.choose_trump(hakem_seat, suit_str)
            if not success:
                return False, "خال نامعتبر است."

            room.seq += 1
            self._persist_room(room)
            await self._broadcast_room_state(room)
            # Check if next turn is AI
            await self._check_ai_turn(room)
            return True, "حکم با موفقیت ثبت شد."

    async def handle_draft_selection(self, room_id: str, user_id: int, chosen_card: str) -> Tuple[bool, str]:
        """Processes 2-Player draft card selection."""
        room = self.rooms.get(room_id)
        if not room or room.game_mode != GameMode.TWO_PLAYER:
            return False, "اتاق یا حالت بازی نامعتبر است."

        async with room.lock:
            engine: TwoPlayerHokmEngine = room.engine  # type: ignore
            cur_seat = engine.state.current_turn_seat
            if cur_seat is None or engine.players[cur_seat].user_id != user_id:
                return False, "نوبت شما نیست."

            success = engine.select_draft_card(cur_seat, chosen_card)
            if not success:
                return False, "کارت نامعتبر است."

            room.seq += 1
            self._persist_room(room)
            await self._broadcast_room_state(room)
            await self._check_ai_turn(room)
            return True, "کارت انتخاب شد."

    async def handle_play_card(self, room_id: str, user_id: int, card_code: str) -> Tuple[bool, str]:
        """Processes playing a card."""
        room = self.rooms.get(room_id)
        if not room:
            return False, "اتاق پیدا نشد."

        async with room.lock:
            cur_seat = room.engine.state.current_turn_seat
            if cur_seat is None or room.engine.players[cur_seat].user_id != user_id:
                return False, "نوبت شما نیست."

            success, error, played, completed_trick, hand_summary = room.engine.play_card(cur_seat, card_code)
            if not success:
                return False, error or "حرکت نامعتبر."

            room.seq += 1

            # Check if match ended
            if room.engine.state.stage == HokmStage.MATCH_ENDED:
                await self._finalize_match(room)
            else:
                self._persist_room(room)

            await self._broadcast_room_state(room)

            # If hand ended, wait 3 seconds to show summary then start next hand
            if hand_summary and room.engine.state.stage == HokmStage.HAND_ENDED:
                asyncio.create_task(self._delay_and_start_next_hand(room_id))
            else:
                # If trick completed or next turn is AI, check AI
                if completed_trick:
                    # Brief pause so players see the 4th/3rd card land before next lead
                    asyncio.create_task(self._delay_and_check_ai(room_id, 0.8))
                else:
                    await self._check_ai_turn(room)

            return True, "کارت با موفقیت بازی شد."

    async def _delay_and_check_ai(self, room_id: str, delay_sec: float) -> None:
        await asyncio.sleep(delay_sec)
        room = self.rooms.get(room_id)
        if room:
            async with room.lock:
                await self._check_ai_turn(room)

    async def _delay_and_start_next_hand(self, room_id: str) -> None:
        await asyncio.sleep(3.5)
        room = self.rooms.get(room_id)
        if not room:
            return
        async with room.lock:
            if room.engine.state.stage != HokmStage.HAND_ENDED:
                return
            room.engine.start_new_hand()
            room.seq += 1
            self._persist_room(room)
            await self._broadcast_room_state(room)
            await self._check_ai_turn(room)

    async def _check_ai_turn(self, room: HokmRoom) -> None:
        """Executes AI actions asynchronously if the current turn belongs to an AI player."""
        engine = room.engine
        stage = engine.state.stage

        if stage == HokmStage.TRUMP_SELECTION:
            hakem_seat = engine.state.hakem_seat
            if hakem_seat is not None and engine.players[hakem_seat].is_ai:
                ai_player = engine.players[hakem_seat]
                best_trump = choose_trump_ai(ai_player.cards)
                await asyncio.sleep(0.6)
                engine.choose_trump(hakem_seat, best_trump)
                room.seq += 1
                self._persist_room(room)
                await self._broadcast_room_state(room)
                await self._check_ai_turn(room)

        elif stage == HokmStage.TWO_PLAYER_DRAFT:
            cur_seat = engine.state.current_turn_seat
            if cur_seat is not None and engine.players[cur_seat].is_ai:
                e2: TwoPlayerHokmEngine = engine  # type: ignore
                if e2.state.draft_offered_cards:
                    chosen = choose_draft_card_ai(e2.state.draft_offered_cards, e2.state.trump_suit)
                    await asyncio.sleep(0.4)
                    e2.select_draft_card(cur_seat, chosen)
                    room.seq += 1
                    self._persist_room(room)
                    await self._broadcast_room_state(room)
                    await self._check_ai_turn(room)

        elif stage == HokmStage.PLAYING:
            cur_seat = engine.state.current_turn_seat
            if cur_seat is not None and engine.players[cur_seat].is_ai:
                ai_player = engine.players[cur_seat]
                private_state = engine.get_private_state(cur_seat)
                legal_moves = private_state["legal_moves"]
                if legal_moves:
                    is_team = room.game_mode == GameMode.FOUR_PLAYER
                    chosen_card = get_ai_action_decision(
                        difficulty=ai_player.ai_difficulty or "medium",
                        legal_moves=legal_moves,
                        current_trick=engine.state.current_trick,
                        completed_tricks=engine.state.completed_tricks,
                        my_seat=cur_seat,
                        trump_suit_str=engine.state.trump_suit,
                        is_team_mode=is_team,
                    )
                    await asyncio.sleep(0.5)
                    success, error, played, completed_trick, hand_summary = engine.play_card(cur_seat, chosen_card)
                    if success:
                        room.seq += 1
                        if engine.state.stage == HokmStage.MATCH_ENDED:
                            await self._finalize_match(room)
                        else:
                            self._persist_room(room)
                        await self._broadcast_room_state(room)

                        if hand_summary and engine.state.stage == HokmStage.HAND_ENDED:
                            asyncio.create_task(self._delay_and_start_next_hand(room.room_id))
                        elif completed_trick:
                            asyncio.create_task(self._delay_and_check_ai(room.room_id, 0.7))
                        else:
                            await self._check_ai_turn(room)

    async def _finalize_match(self, room: HokmRoom) -> None:
        """Handles match finish, Elo ratings update, and stats persistence."""
        engine = room.engine
        mode_val = room.game_mode.value
        winner_team = engine.state.match_winner_team
        winner_seat = engine.state.match_winner_seat

        final_score = (
            f"{engine.state.team_a_match_points}-{engine.state.team_b_match_points}"
            if room.game_mode == GameMode.FOUR_PLAYER
            else "-".join(str(p.match_score) for p in engine.players)
        )

        winner_id = None
        if winner_seat is not None:
            winner_id = engine.players[winner_seat].user_id

        # Record match in database
        self.repo.record_match_history(
            room_id=room.room_id,
            game_type=mode_val,
            winner_id=winner_id,
            winner_team=winner_team,
            final_score=final_score,
            hands_played=engine.state.hand_number,
        )

        # Update Elo ratings for ranked human matches
        if room.is_ranked and not any(p.is_ai for p in engine.players):
            if room.game_mode == GameMode.FOUR_PLAYER and winner_team is not None:
                winners = [p.user_id for p in engine.players if p.team == winner_team]
                losers = [p.user_id for p in engine.players if p.team != winner_team]
                self.repo.update_rating_match(winners, losers, mode_val)
            elif winner_seat is not None:
                winner_uid = engine.players[winner_seat].user_id
                loser_uids = [p.user_id for p in engine.players if p.seat != winner_seat]
                self.repo.update_rating_match([winner_uid], loser_uids, mode_val)

        room.updated_at = time.time()
        self._persist_room(room)

    async def create_solo_ai_game(
        self,
        host_id: int,
        host_name: str,
        mode_str: str = "hokm_4p",
        difficulty: str = "medium",
        turn_time_limit: int = 30,
    ) -> HokmRoom:
        """Instantly creates a room, fills other seats with AI bots, and starts the game."""
        room = await self.create_room(
            host_id=host_id,
            host_name=host_name,
            mode_str=mode_str,
            turn_time_limit=turn_time_limit,
            is_ranked=False,
            allow_spectators=False,
        )
        # Fill remaining open seats with AI
        for _ in range(1, room.game_mode.player_count):
            await self.add_ai_player(room.room_id, host_id, difficulty)

        # Start game immediately
        await self.start_game(room.room_id, host_id)
        return room

    async def _broadcast_room_state(self, room: HokmRoom) -> None:
        """Sends sanitized public and private states to all room participants."""
        public_state = room.engine.get_public_state()
        public_state["invite_code"] = room.invite_code
        public_state["room_id"] = room.room_id
        public_state["seq"] = room.seq
        connected_humans = {
            player.user_id
            for player in room.engine.players
            if not player.is_ai and player.user_id in self.manager.get_room_members(room.room_id)
        }
        public_state["rematch_votes"] = sorted(room.rematch_votes)
        public_state["rematch_required"] = (
            len(connected_humans)
            if room.engine.state.stage == HokmStage.MATCH_ENDED
            else 0
        )

        # If in waiting stage before game starts, populate players from seat_slots
        if room.engine.state.stage == HokmStage.WAITING:
            public_state["players"] = [
                {
                    "seat": idx,
                    "user_id": slot["user_id"],
                    "name": slot["name"],
                    "team": slot.get("team"),
                    "is_ai": slot.get("is_ai", False),
                    "ai_difficulty": slot.get("ai_difficulty"),
                    "cards_remaining": 0,
                    "tricks_won": 0,
                    "is_connected": True,
                }
                if slot
                else None
                for idx, slot in enumerate(room.seat_slots)
            ]

        # Gather all human recipient user_ids
        human_uids = set()
        for slot in room.seat_slots:
            if slot and not slot.get("is_ai"):
                human_uids.add(slot["user_id"])
        for p in room.engine.players:
            if not p.is_ai:
                human_uids.add(p.user_id)
        for spec_uid in room.spectators:
            human_uids.add(spec_uid)

        # Include all connected WS members in the room
        room_members = self.manager.get_room_members(room.room_id)
        human_uids.update(room_members)

        for uid in human_uids:
            seat = -1
            if room.engine.players:
                for p in room.engine.players:
                    if p.user_id == uid:
                        seat = p.seat
                        break
            else:
                for idx, slot in enumerate(room.seat_slots):
                    if slot and slot["user_id"] == uid:
                        seat = idx
                        break

            private_state = (
                room.engine.get_private_state(seat)
                if seat != -1 and room.engine.players
                else {"seat": seat, "cards": [], "legal_moves": []}
            )

            payload = {
                "type": "hokm.game.state",
                "room_id": room.room_id,
                "seq": room.seq,
                "public": public_state,
                "private": private_state,
            }
            await self.manager.send_to_user(uid, payload)

    async def handle_disconnect(self, room_id: str, user_id: int) -> None:
        """Marks player disconnected and starts 90-second grace period."""
        room = self.rooms.get(room_id)
        if not room:
            return
        async with room.lock:
            for p in room.engine.players:
                if p.user_id == user_id:
                    p.is_connected = False
                    p.disconnect_time = time.time()
                    room.seq += 1
                    self._persist_room(room)
                    await self._broadcast_room_state(room)
                    break
            else:
                for slot in room.seat_slots:
                    if slot and slot.get("user_id") == user_id and not slot.get("is_ai"):
                        slot["is_connected"] = False
                        room.seq += 1
                        self._persist_room(room)
                        await self._broadcast_room_state(room)
                        break

    async def check_timeouts(self) -> None:
        """Schedule at most one timeout resolver per room."""
        now = time.time()
        for room in list(self.rooms.values()):
            if room.engine.state.stage not in (
                HokmStage.PLAYING,
                HokmStage.TRUMP_SELECTION,
                HokmStage.TWO_PLAYER_DRAFT,
            ):
                continue
            deadline = room.engine.state.action_deadline
            if deadline is None or now <= deadline:
                continue

            pending = self._timeout_tasks.get(room.room_id)
            if pending is not None and not pending.done():
                continue
            task = asyncio.create_task(self._resolve_timeout_once(room))
            self._timeout_tasks[room.room_id] = task

    async def _resolve_timeout_once(self, room: HokmRoom) -> None:
        try:
            await self._resolve_timeout(room)
        finally:
            current = asyncio.current_task()
            if self._timeout_tasks.get(room.room_id) is current:
                self._timeout_tasks.pop(room.room_id, None)

    async def _resolve_timeout(self, room: HokmRoom) -> None:
        async with room.lock:
            engine = room.engine
            stage = engine.state.stage
            cur_seat = engine.state.current_turn_seat
            deadline = engine.state.action_deadline
            if deadline is None or time.time() < deadline:
                return

            if cur_seat is None:
                return

            if stage == HokmStage.TRUMP_SELECTION:
                # Default to Spades if Hakem times out
                if engine.choose_trump(cur_seat, "S"):
                    room.seq += 1
                    self._persist_room(room)
                    await self._broadcast_room_state(room)
                await self._check_ai_turn(room)

            elif stage == HokmStage.TWO_PLAYER_DRAFT:
                e2: TwoPlayerHokmEngine = engine  # type: ignore
                if e2.state.draft_offered_cards:
                    if e2.select_draft_card(cur_seat, e2.state.draft_offered_cards[0]):
                        room.seq += 1
                        self._persist_room(room)
                        await self._broadcast_room_state(room)
                    await self._check_ai_turn(room)

            elif stage == HokmStage.PLAYING:
                private_state = engine.get_private_state(cur_seat)
                legal_moves = private_state["legal_moves"]
                if legal_moves:
                    # Pick lowest legal card on timeout
                    cards = [Card.from_code(c) for c in legal_moves]
                    lowest_card = min(cards, key=lambda c: c.value).code
                    success, _, _, completed_trick, hand_summary = engine.play_card(cur_seat, lowest_card)
                    if success:
                        room.seq += 1
                        if engine.state.stage == HokmStage.MATCH_ENDED:
                            await self._finalize_match(room)
                        else:
                            self._persist_room(room)
                        await self._broadcast_room_state(room)

                        if hand_summary and engine.state.stage == HokmStage.HAND_ENDED:
                            asyncio.create_task(self._delay_and_start_next_hand(room.room_id))
                        elif completed_trick:
                            asyncio.create_task(self._delay_and_check_ai(room.room_id, 0.7))
                        else:
                            await self._check_ai_turn(room)
