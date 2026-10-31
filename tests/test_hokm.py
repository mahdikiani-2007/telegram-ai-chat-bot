import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import time
from games.hokm.ai.strategy import choose_card_easy, choose_card_hard, choose_card_medium, choose_draft_card_ai, choose_trump_ai
from games.hokm.service import HokmService
from games.hokm.four_player.engine import FourPlayerHokmEngine
from games.hokm.shared.cards import Card, Rank, Suit
from games.hokm.shared.deck import create_3p_51_deck, create_standard_52_deck
from games.hokm.shared.models import GameMode, HokmStage, PlayedCard
from games.hokm.shared.scoring import evaluate_hand_result
from games.hokm.shared.shuffle import secure_shuffle
from games.hokm.shared.tricks import get_legal_moves, resolve_trick, validate_card_play
from games.hokm.three_player.engine import ThreePlayerHokmEngine
from games.hokm.two_player.engine import TwoPlayerHokmEngine
from realtime.manager import ConnectionManager
from repositories.games import GameRepository


class _DummyWebSocket:
    async def close(self, code=1000, reason=""):
        return None

    async def send_json(self, message):
        return None


def test_deck_and_cards():
    deck52 = create_standard_52_deck()
    assert len(deck52) == 52
    assert len(set(c.code for c in deck52)) == 52

    deck51 = create_3p_51_deck()
    assert len(deck51) == 51
    assert "2C" not in [c.code for c in deck51]

    # Test Card hierarchy
    ace_spades = Card.from_code("AS")
    king_spades = Card.from_code("KS")
    two_spades = Card.from_code("2S")
    ace_hearts = Card.from_code("AH")

    assert ace_spades.value == 14
    assert king_spades.value == 13
    assert two_spades.value == 2

    # Trump beats non-trump
    assert ace_hearts.beats(ace_spades, led_suit=Suit.SPADES, trump_suit=Suit.HEARTS) is True
    # Highest card of led suit beats lower card of led suit
    assert ace_spades.beats(king_spades, led_suit=Suit.SPADES, trump_suit=Suit.HEARTS) is True
    # Non-trump off-suit cannot beat led suit
    ten_diamonds = Card.from_code("10D")
    assert ten_diamonds.beats(two_spades, led_suit=Suit.SPADES, trump_suit=Suit.HEARTS) is False


def test_secure_shuffle():
    deck = create_standard_52_deck()
    shuffled, seed, commitment = secure_shuffle(deck)
    assert len(shuffled) == 52
    assert len(set(c.code for c in shuffled)) == 52
    assert len(seed) == 64
    assert len(commitment) == 64


def test_four_player_engine_complete_hand():
    engine = FourPlayerHokmEngine("room_4p_test")
    engine.initialize_players([{"user_id": i, "name": f"P{i}"} for i in range(1, 5)])

    # Ceremony
    drawn = engine.start_ceremony()
    assert len(drawn) == 4
    assert engine.state.hakem_seat is not None
    hakem = engine.state.hakem_seat

    # Start new hand -> 5 cards to each
    engine.start_new_hand()
    for p in engine.players:
        assert len(p.cards) == 5
    assert engine.state.stage == HokmStage.TRUMP_SELECTION

    # Choose trump -> deals remaining 4 + 4 cards
    assert engine.choose_trump(hakem, "H") is True
    assert engine.state.trump_suit == "H"
    for p in engine.players:
        assert len(p.cards) == 13
    assert engine.state.stage == HokmStage.PLAYING

    # Play tricks until hand ends
    tricks_played = 0
    while engine.state.stage == HokmStage.PLAYING:
        cur_seat = engine.state.current_turn_seat
        assert cur_seat is not None
        p_state = engine.get_private_state(cur_seat)
        legal = p_state["legal_moves"]
        assert len(legal) > 0
        card_to_play = legal[0]
        ok, err, played, completed_trick, summary = engine.play_card(cur_seat, card_to_play)
        assert ok is True
        if completed_trick:
            tricks_played += 1

    # Team reached 7 tricks
    team_a_tricks = engine.players[0].tricks_won + engine.players[2].tricks_won
    team_b_tricks = engine.players[1].tricks_won + engine.players[3].tricks_won
    assert max(team_a_tricks, team_b_tricks) >= 7
    assert team_a_tricks + team_b_tricks == tricks_played
    assert engine.state.stage in (HokmStage.HAND_ENDED, HokmStage.MATCH_ENDED)
    public = engine.get_public_state()
    assert len(public["last_completed_trick"]["cards"]) == 4
    assert all("cards" not in player for player in public["players"])


def test_three_player_engine_distribution_and_play():
    engine = ThreePlayerHokmEngine("room_3p_test")
    engine.initialize_players([{"user_id": i, "name": f"P{i}"} for i in range(1, 4)])

    drawn = engine.start_ceremony()
    assert len(drawn) == 3
    hakem = engine.state.hakem_seat

    engine.start_new_hand()
    for p in engine.players:
        assert len(p.cards) == 5

    # Choose trump: deals 4 + 4 + 4 = 12 more cards -> 17 total each
    assert engine.choose_trump(hakem, "S") is True
    assert engine.state.trump_suit == "S"
    for p in engine.players:
        assert len(p.cards) == 17
    assert engine.state.stage == HokmStage.PLAYING


def test_two_player_draft_and_hand_size():
    engine = TwoPlayerHokmEngine("room_2p_test")
    engine.initialize_players([{"user_id": i, "name": f"P{i}"} for i in range(1, 3)])

    drawn = engine.start_ceremony()
    assert len(drawn) == 2
    hakem = engine.state.hakem_seat

    engine.start_new_hand()
    assert len(engine.players[0].cards) == 5
    assert len(engine.players[1].cards) == 5
    assert len(engine.state.stock_cards) == 42

    assert engine.choose_trump(hakem, "D") is True
    assert engine.state.stage == HokmStage.TWO_PLAYER_DRAFT

    # 16 selection turns (8 each)
    for turn in range(16):
        cur_seat = engine.state.current_turn_seat
        assert cur_seat is not None
        p_state = engine.get_private_state(cur_seat)
        offered = p_state["draft_offered_cards"]
        assert offered is not None and len(offered) == 2
        # Choose 1
        card_to_keep = offered[0]
        assert engine.select_draft_card(cur_seat, card_to_keep) is True

    # After draft, both have exactly 13 cards, and 10 remain unused in stock!
    assert len(engine.players[0].cards) == 13
    assert len(engine.players[1].cards) == 13
    assert len(engine.state.stock_cards) == 10
    assert len(engine.state.discard_cards) == 16
    assert engine.state.stage == HokmStage.PLAYING


def _play_engine_to_hand_end(engine, max_moves: int = 120):
    moves = 0
    while engine.state.stage == HokmStage.PLAYING:
        seat = engine.state.current_turn_seat
        assert seat is not None
        legal = engine.get_private_state(seat)["legal_moves"]
        assert legal
        success, error, _played, _trick, _summary = engine.play_card(seat, legal[0])
        assert success, error
        moves += 1
        assert moves <= max_moves
    return moves


def test_two_player_engine_complete_hand_and_trick_snapshot():
    engine = TwoPlayerHokmEngine("room_2p_full")
    engine.initialize_players([{"user_id": 11, "name": "A"}, {"user_id": 12, "name": "B"}])
    engine.start_ceremony()
    engine.start_new_hand()
    assert engine.choose_trump(engine.state.hakem_seat, "H")

    for _ in range(16):
        seat = engine.state.current_turn_seat
        offered = engine.get_private_state(seat)["draft_offered_cards"]
        assert offered and len(offered) == 2
        assert engine.select_draft_card(seat, offered[0])

    assert engine.state.stage == HokmStage.PLAYING
    assert all(len(player.cards) == 13 for player in engine.players)
    moves = _play_engine_to_hand_end(engine, max_moves=26)
    assert moves % 2 == 0 and moves <= 26
    assert engine.state.stage in (HokmStage.HAND_ENDED, HokmStage.MATCH_ENDED)
    assert engine.last_hand_summary is not None
    assert sum(player.tricks_won for player in engine.players) == moves // 2
    assert max(player.tricks_won for player in engine.players) >= 7
    public = engine.get_public_state()
    assert len(public["last_completed_trick"]["cards"]) == 2
    assert "cards" not in public["players"][0]


def test_three_player_engine_complete_hand_and_draw_scoring():
    engine = ThreePlayerHokmEngine("room_3p_full")
    engine.initialize_players([{"user_id": i, "name": f"P{i}"} for i in range(3)])
    engine.start_ceremony()
    engine.start_new_hand()
    assert engine.choose_trump(engine.state.hakem_seat, "S")
    assert all(len(player.cards) == 17 for player in engine.players)

    moves = _play_engine_to_hand_end(engine, max_moves=51)
    assert moves % 3 == 0 and moves <= 51
    assert engine.state.stage in (HokmStage.HAND_ENDED, HokmStage.MATCH_ENDED)
    assert engine.last_hand_summary is not None
    total_tricks = sum(player.tricks_won for player in engine.players)
    assert total_tricks == moves // 3
    assert total_tricks >= 9 or total_tricks == 17
    public = engine.get_public_state()
    assert len(public["last_completed_trick"]["cards"]) == 3
    assert all("cards" not in player for player in public["players"])


@pytest.mark.anyio
async def test_hokm_private_snapshot_recovers_waiting_and_dealt_hands(tmp_path):
    db_file = str(tmp_path / "hokm_recovery.db")
    repo = GameRepository(db_file)
    service = HokmService(repo, ConnectionManager())

    room = await service.create_room(501, "Host", "hokm_4p")
    ok, _message, joined = await service.join_room(502, "Guest", room.invite_code)
    assert ok and joined is room
    assert await service.add_ai_player(room.room_id, 501, "easy") == (True, "ربات اضافه شد.")
    assert await service.add_ai_player(room.room_id, 501, "medium") == (True, "ربات اضافه شد.")

    room.engine.initialize_players(room.seat_slots)
    room.engine.start_ceremony()
    room.engine.start_new_hand()
    room.seq += 1
    service._persist_room(room)

    recovered_service = HokmService(GameRepository(db_file), ConnectionManager())
    assert await recovered_service.recover_from_db() == 1
    recovered = recovered_service.rooms[room.room_id]
    assert recovered.engine.state.stage == HokmStage.TRUMP_SELECTION
    assert len(recovered.engine.players) == 4
    assert all(len(player.cards) == 5 for player in recovered.engine.players)
    assert recovered.engine.players[2].is_ai is True
    assert recovered.engine.players[3].is_ai is True
    assert recovered.engine.players[0].is_connected is False
    assert recovered.engine.players[1].is_connected is False
    assert recovered.seat_slots[0]["is_connected"] is False
    assert recovered.engine.get_private_state(0)["cards"] == recovered.engine.players[0].cards
    assert "stock_cards" not in recovered.engine.get_public_state()


@pytest.mark.anyio
async def test_hokm_waiting_room_leave_and_unanimous_rematch(tmp_path):
    db_file = str(tmp_path / "hokm_room_lifecycle.db")
    repo = GameRepository(db_file)
    manager = ConnectionManager()
    service = HokmService(repo, manager)
    await manager.connect(_DummyWebSocket(), 601, "Host")
    await manager.connect(_DummyWebSocket(), 602, "Guest")

    waiting = await service.create_room(601, "Host", "hokm_2p")
    await manager.join_room(601, waiting.room_id)
    ok, _message, room = await service.join_room(602, "Guest", waiting.invite_code)
    assert ok and room is waiting
    await manager.join_room(602, room.room_id)
    leave_ok, _ = await service.leave_room(room.room_id, 602)
    assert leave_ok is True
    assert room.seat_slots[1] is None

    # Rejoin and put the room into a completed-match state.
    ok, _message, room = await service.join_room(602, "Guest", waiting.invite_code)
    assert ok
    await manager.join_room(602, room.room_id)
    room.engine.initialize_players(room.seat_slots)
    room.engine.state.stage = HokmStage.MATCH_ENDED
    room.engine.state.match_winner_seat = 0
    service._persist_room(room)

    first_vote, first_message, new_room = await service.request_rematch(room.room_id, 601)
    assert first_vote is False and new_room is None
    assert "1/2" in first_message

    second_vote, _message, new_room = await service.request_rematch(room.room_id, 602)
    assert second_vote is True and new_room is not None
    assert new_room.room_id != room.room_id
    assert new_room.engine.state.stage == HokmStage.CEREMONY
    assert [player.user_id for player in new_room.engine.players] == [601, 602]


def test_ai_strategies():
    legal = ["2H", "10H", "AH", "5S", "KS"]

    # Easy
    easy_choice = choose_card_easy(legal)
    assert easy_choice == "2H"

    # Medium
    med_lead = choose_card_medium(legal, [], "H")
    assert med_lead in legal

    # Trump selection AI
    hand = ["AS", "KS", "QS", "2S", "5D"]
    best_trump = choose_trump_ai(hand)
    assert best_trump == "S"

    # 2P draft AI
    draft_choice = choose_draft_card_ai(["AS", "2C"], "S")
    assert draft_choice == "AS"


@pytest.mark.anyio
async def test_hokm_disconnect_grace_replaces_seat_with_ai(tmp_path):
    repo = GameRepository(str(tmp_path / "hokm_disconnect_grace.db"))
    manager = ConnectionManager()
    service = HokmService(repo, manager)

    ids = [901, 902, 903, 904]
    for uid in ids:
        await manager.connect(_DummyWebSocket(), uid, f"P{uid}")

    room = await service.create_room(ids[0], "P901", "hokm_4p")
    await manager.join_room(ids[0], room.room_id)
    for uid in ids[1:]:
        ok, _message, joined = await service.join_room(uid, f"P{uid}", room.invite_code)
        assert ok and joined is room
        await manager.join_room(uid, room.room_id)

    room.engine.initialize_players(room.seat_slots)
    room.engine.start_ceremony()
    room.engine.start_new_hand()
    hakem = room.engine.state.hakem_seat
    assert room.engine.choose_trump(hakem, "S") is True

    disconnected = room.engine.players[1]
    former_user_id = disconnected.user_id
    original_cards = list(disconnected.cards)
    disconnected.is_connected = False
    disconnected.disconnect_time = time.time() - 91
    room.seat_slots[1]["is_connected"] = False
    room.engine.state.current_turn_seat = 0
    room.engine.state.action_deadline = time.time() + 30
    service._persist_room(room)

    await service.check_timeouts()

    replacement = room.engine.players[1]
    assert replacement.is_ai is True
    assert replacement.ai_difficulty == "medium"
    assert replacement.user_id < 0
    assert replacement.cards == original_cards
    assert room.seat_slots[1]["is_ai"] is True
    assert former_user_id not in service._user_to_room
    assert former_user_id not in manager.get_room_members(room.room_id)
    assert room.engine.get_public_state()["players"][1]["is_ai"] is True


@pytest.mark.anyio
async def test_hokm_user_cannot_join_a_second_active_room(tmp_path):
    repo = GameRepository(str(tmp_path / "hokm_active_room_guard.db"))
    service = HokmService(repo, ConnectionManager())

    first = await service.create_room(911, "Host 1", "hokm_2p")
    ok, _message, first_room = await service.join_room(912, "Player", first.invite_code)
    assert ok and first_room is not None
    first_room.engine.initialize_players(first_room.seat_slots)
    first_room.engine.state.stage = HokmStage.PLAYING
    service._persist_room(first_room)

    second = await service.create_room(913, "Host 2", "hokm_2p")
    ok, message, joined = await service.join_room(912, "Player", second.invite_code)
    assert ok is False
    assert joined is None
    assert "فعال" in message
    assert service._user_to_room[912] == first_room.room_id


@pytest.mark.anyio
async def test_hokm_host_cancel_cleans_all_seats_and_allows_new_room(tmp_path):
    repo = GameRepository(str(tmp_path / "hokm_cancel_room.db"))
    manager = ConnectionManager()
    service = HokmService(repo, manager)
    host_ws = _DummyWebSocket()
    guest_ws = _DummyWebSocket()
    await manager.connect(host_ws, 701, "Host")
    await manager.connect(guest_ws, 702, "Guest")

    room = await service.create_room(701, "Host", "hokm_3p")
    await manager.join_room(701, room.room_id)
    ok, _message, joined = await service.join_room(702, "Guest", room.invite_code)
    assert ok and joined is room
    await manager.join_room(702, room.room_id)

    left, _message = await service.leave_room(room.room_id, 701)
    assert left is True
    assert room.invite_code not in service.code_to_room
    assert service._user_to_room.get(701) is None
    assert service._user_to_room.get(702) is None
    assert not manager.get_room_members(room.room_id)
    assert repo.get_room(room.room_id)["status"] == "abandoned"

    next_room = await service.create_room(701, "Host", "hokm_3p")
    assert next_room.room_id != room.room_id
    assert next_room.engine.state.stage == HokmStage.WAITING


@pytest.mark.anyio
async def test_completed_hokm_match_recovers_for_rematch_after_restart(tmp_path):
    db_file = str(tmp_path / "hokm_completed_recovery.db")
    repo = GameRepository(db_file)
    manager = ConnectionManager()
    service = HokmService(repo, manager)

    room = await service.create_room(921, "Host", "hokm_2p")
    ok, _message, room = await service.join_room(922, "Guest", room.invite_code)
    assert ok and room is not None
    room.engine.initialize_players(room.seat_slots)
    room.engine.state.stage = HokmStage.MATCH_ENDED
    room.engine.state.match_winner_seat = 0
    service._persist_room(room)
    assert repo.get_room(room.room_id)["status"] == "completed"

    recovered_manager = ConnectionManager()
    await recovered_manager.connect(_DummyWebSocket(), 921, "Host")
    await recovered_manager.connect(_DummyWebSocket(), 922, "Guest")
    recovered_service = HokmService(GameRepository(db_file), recovered_manager)

    assert await recovered_service.recover_from_db() == 1
    recovered = recovered_service.rooms[room.room_id]
    assert recovered.engine.state.stage == HokmStage.MATCH_ENDED
    assert recovered.engine.players[0].user_id == 921
    assert recovered.engine.players[1].user_id == 922
    assert recovered.engine.players[0].is_connected is False

    await recovered_manager.join_room(921, recovered.room_id)
    await recovered_manager.join_room(922, recovered.room_id)
    first_vote, _message, new_room = await recovered_service.request_rematch(recovered.room_id, 921)
    assert first_vote is False and new_room is None

    second_vote, _message, new_room = await recovered_service.request_rematch(recovered.room_id, 922)
    assert second_vote is True and new_room is not None
    assert new_room.engine.state.stage == HokmStage.CEREMONY
    assert [player.user_id for player in new_room.engine.players] == [921, 922]
    assert all(player.is_connected for player in new_room.engine.players)


@pytest.mark.anyio
async def test_hokm_player_cannot_join_two_active_rooms(tmp_path):
    repo = GameRepository(str(tmp_path / "hokm_multi_room.db"))
    service = HokmService(repo, ConnectionManager())

    first = await service.create_room(801, "Host 1", "hokm_2p")
    ok, _message, _ = await service.join_room(802, "Player", first.invite_code)
    assert ok

    first.engine.initialize_players(first.seat_slots)
    first.engine.state.stage = HokmStage.PLAYING
    service._persist_room(first)

    second = await service.create_room(803, "Host 2", "hokm_2p")
    ok, message, joined = await service.join_room(802, "Player", second.invite_code)
    assert ok is False
    assert joined is None
    assert "فعال" in message
    assert service._user_to_room[802] == first.room_id


def test_elo_ratings(tmp_path):
    db_file = str(tmp_path / "test_games.db")
    repo = GameRepository(db_file)

    p1 = repo.get_or_create_rating(101, "hokm_4p")
    assert p1["rating"] == 1000
    assert p1["tier"] == "Bronze"

    # Win match
    results = repo.update_rating_match([101, 102], [201, 202], "hokm_4p")
    assert results[101]["new_rating"] > 1000
    assert results[201]["new_rating"] < 1000

    # Test tier mapping
    assert repo.rating_to_tier(1050) == "Bronze"
    assert repo.rating_to_tier(1200) == "Silver"
    assert repo.rating_to_tier(1400) == "Gold"
    assert repo.rating_to_tier(1600) == "Platinum"
    assert repo.rating_to_tier(1800) == "Diamond"
    assert repo.rating_to_tier(2000) == "Master"


@pytest.mark.anyio
async def test_hokm_completed_trick_locks_next_move_until_resolution_window(tmp_path):
    repo = GameRepository(str(tmp_path / "hokm_trick_resolution.db"))
    service = HokmService(repo, ConnectionManager())
    room = await service.create_room(951, "Host", "hokm_2p")
    ok, _message, joined = await service.join_room(952, "Guest", room.invite_code)
    assert ok and joined is room

    engine = room.engine
    engine.initialize_players(room.seat_slots)
    engine.start_ceremony()
    engine.start_new_hand()
    assert engine.choose_trump(engine.state.hakem_seat, "S") is True

    for _ in range(16):
        seat = engine.state.current_turn_seat
        assert seat is not None
        offered = engine.state.draft_offered_cards
        assert offered
        assert engine.select_draft_card(seat, offered[0]) is True

    assert engine.state.stage == HokmStage.PLAYING

    first_seat = engine.state.current_turn_seat
    assert first_seat is not None
    first_card = engine.get_private_state(first_seat)["legal_moves"][0]
    first_ok, _ = await service.handle_play_card(
        room.room_id,
        engine.players[first_seat].user_id,
        first_card,
    )
    assert first_ok is True

    second_seat = engine.state.current_turn_seat
    assert second_seat is not None
    second_card = engine.get_private_state(second_seat)["legal_moves"][0]
    second_ok, _ = await service.handle_play_card(
        room.room_id,
        engine.players[second_seat].user_id,
        second_card,
    )
    assert second_ok is True
    assert room.trick_resolution_until > time.time()
    assert len(engine.state.current_trick) == 0

    next_seat = engine.state.current_turn_seat
    assert next_seat is not None
    next_card = engine.get_private_state(next_seat)["legal_moves"][0]
    cards_before = list(engine.players[next_seat].cards)

    blocked, message = await service.handle_play_card(
        room.room_id,
        engine.players[next_seat].user_id,
        next_card,
    )
    assert blocked is False
    assert "جمع" in message
    assert engine.players[next_seat].cards == cards_before

    room.trick_resolution_until = time.time() - 0.01
    accepted, _ = await service.handle_play_card(
        room.room_id,
        engine.players[next_seat].user_id,
        next_card,
    )
    assert accepted is True
    assert next_card not in engine.players[next_seat].cards
