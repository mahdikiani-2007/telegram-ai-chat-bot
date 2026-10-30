import time
from typing import Any, Dict, List, Optional, Tuple

from games.hokm.shared.cards import Card, Suit
from games.hokm.shared.deck import create_standard_52_deck
from games.hokm.shared.models import (
    GameMode,
    HandSummary,
    HokmGameState,
    HokmStage,
    PlayedCard,
    PlayerSeat,
    Trick,
)
from games.hokm.shared.scoring import calculate_next_hakem, evaluate_hand_result
from games.hokm.shared.shuffle import secure_shuffle
from games.hokm.shared.tricks import get_legal_moves, resolve_trick, validate_card_play


class FourPlayerHokmEngine:
    """Authoritative game engine for traditional 4-player team-based Hokm."""

    def __init__(self, room_id: str, turn_time_limit: int = 30):
        self.room_id = room_id
        self.players: List[PlayerSeat] = []
        self.state = HokmGameState(
            game_mode=GameMode.FOUR_PLAYER,
            turn_time_limit=turn_time_limit,
        )
        self.last_hand_summary: Optional[HandSummary] = None

    def initialize_players(self, player_configs: List[Dict[str, Any]]) -> None:
        """Sets up 4 players with alternating teams (Team A: seats 0, 2; Team B: seats 1, 3)."""
        self.players = []
        for i, cfg in enumerate(player_configs[:4]):
            team = 0 if i in (0, 2) else 1
            self.players.append(
                PlayerSeat(
                    seat=i,
                    user_id=cfg["user_id"],
                    name=cfg.get("name", f"Player {i+1}"),
                    team=team,
                    is_ai=cfg.get("is_ai", False),
                    ai_difficulty=cfg.get("ai_difficulty"),
                    cards=[],
                    tricks_won=0,
                    is_connected=True,
                )
            )

    def start_ceremony(self) -> Dict[int, str]:
        """Performs initial Hakem selection ceremony by drawing 1 card per player.
        Player holding the highest card (Spades > Hearts > Diamonds > Clubs tiebreak) becomes Hakem.
        """
        deck = create_standard_52_deck()
        shuffled, seed, commitment = secure_shuffle(deck)
        self.state.shuffle_commitment = commitment
        self.state.shuffle_seed = seed

        drawn_cards: Dict[int, Card] = {}
        drawn_codes: Dict[int, str] = {}
        for i in range(4):
            card = shuffled[i]
            drawn_cards[i] = card
            drawn_codes[i] = card.code

        # Find seat with highest card
        best_seat = 0
        best_card = drawn_cards[0]
        for seat in range(1, 4):
            candidate = drawn_cards[seat]
            if candidate.ceremony_compare(best_card) > 0:
                best_seat = seat
                best_card = candidate

        self.state.ceremony_cards = drawn_codes
        self.state.hakem_seat = best_seat
        self.state.stage = HokmStage.CEREMONY
        return drawn_codes

    def start_new_hand(self) -> None:
        """Shuffles a fresh deck, deals 5 cards to each player, and awaits Hakem's trump selection."""
        if self.state.hakem_seat is None:
            raise ValueError("Cannot start hand without Hakem")

        # Reset hand stats
        for p in self.players:
            p.cards = []
            p.tricks_won = 0

        self.state.trump_suit = None
        self.state.current_trick = []
        self.state.completed_tricks = []
        self.state.lead_suit = None

        deck = create_standard_52_deck()
        shuffled, seed, commitment = secure_shuffle(deck)
        self.state.shuffle_commitment = commitment
        self.state.shuffle_seed = seed

        # Stage 1: Deal 5 cards to each player (counterclockwise from Hakem)
        idx = 0
        hakem = self.state.hakem_seat
        for step in range(4):
            seat = (hakem + step) % 4
            self.players[seat].cards = [c.code for c in shuffled[idx : idx + 5]]
            idx += 5

        # Store remaining 32 cards in stock
        self.state.stock_cards = [c.code for c in shuffled[20:]]

        self.state.stage = HokmStage.TRUMP_SELECTION
        self.state.current_turn_seat = self.state.hakem_seat
        self._reset_deadline()

    def choose_trump(self, seat: int, suit_str: str) -> bool:
        """Hakem chooses trump suit after reviewing their first 5 cards."""
        if self.state.stage != HokmStage.TRUMP_SELECTION:
            return False
        if seat != self.state.hakem_seat:
            return False

        try:
            suit = Suit(suit_str.upper())
        except ValueError:
            return False

        self.state.trump_suit = suit.value

        # Complete distribution: Deal 4 cards, then 4 cards to each player
        # (4 players * 8 cards = 32 remaining stock cards)
        shuffled_stock = self.state.stock_cards
        idx = 0
        hakem = self.state.hakem_seat

        # Stage 2: 4 cards each
        for step in range(4):
            s = (hakem + step) % 4
            self.players[s].cards.extend(shuffled_stock[idx : idx + 4])
            idx += 4

        # Stage 3: 4 cards each
        for step in range(4):
            s = (hakem + step) % 4
            self.players[s].cards.extend(shuffled_stock[idx : idx + 4])
            idx += 4

        self.state.stock_cards = []

        # Sort each player's hand for clean UX
        for p in self.players:
            self._sort_hand(p)

        # Game begins: Hakem leads the first trick
        self.state.stage = HokmStage.PLAYING
        self.state.current_turn_seat = self.state.hakem_seat
        self._reset_deadline()
        return True

    def play_card(
        self, seat: int, card_code: str
    ) -> Tuple[bool, Optional[str], Optional[PlayedCard], Optional[Trick], Optional[HandSummary]]:
        """Plays a card for `seat`. Returns (success, error_msg, played_card, completed_trick, hand_summary)."""
        if self.state.stage != HokmStage.PLAYING:
            return False, "بازی در جریان نیست.", None, None, None
        if seat != self.state.current_turn_seat:
            return False, "نوبت شما نیست.", None, None, None

        player = self.players[seat]
        is_legal, error = validate_card_play(player.cards, card_code, self.state.current_trick)
        if not is_legal:
            return False, error, None, None, None

        # Remove card from hand
        player.cards.remove(card_code)
        played = PlayedCard(seat=seat, player_id=player.user_id, card=card_code)
        self.state.current_trick.append(played)

        if len(self.state.current_trick) == 1:
            self.state.lead_suit = Card.from_code(card_code).suit.value

        # Check if trick is complete (all 4 players played)
        if len(self.state.current_trick) == 4:
            winning_play = resolve_trick(self.state.current_trick, self.state.trump_suit)
            winning_seat = winning_play.seat
            self.players[winning_seat].tricks_won += 1

            completed_trick = Trick(
                lead_seat=self.state.current_trick[0].seat,
                lead_card=self.state.current_trick[0].card,
                cards=list(self.state.current_trick),
                winner_seat=winning_seat,
                winner_id=winning_play.player_id,
            )
            self.state.completed_tricks.append(completed_trick)
            self.state.current_trick = []
            self.state.lead_suit = None

            # Check if hand ended (one team reached 7 tricks)
            hand_summary = evaluate_hand_result(
                game_mode=GameMode.FOUR_PLAYER,
                hakem_seat=self.state.hakem_seat,
                trump_suit=self.state.trump_suit,
                hand_number=self.state.hand_number,
                players=self.players,
            )

            if hand_summary:
                self.last_hand_summary = hand_summary
                if hand_summary.winner_team == 0:
                    self.state.team_a_match_points += hand_summary.points_awarded
                else:
                    self.state.team_b_match_points += hand_summary.points_awarded

                # Check if match ended (reached 7 points)
                if self.state.team_a_match_points >= self.state.target_match_points:
                    self.state.stage = HokmStage.MATCH_ENDED
                    self.state.match_winner_team = 0
                elif self.state.team_b_match_points >= self.state.target_match_points:
                    self.state.stage = HokmStage.MATCH_ENDED
                    self.state.match_winner_team = 1
                else:
                    self.state.stage = HokmStage.HAND_ENDED
                    self.state.hakem_seat = calculate_next_hakem(
                        GameMode.FOUR_PLAYER, self.state.hakem_seat, hand_summary
                    )
                    self.state.hand_number += 1

                self.state.action_deadline = None
                return True, None, played, completed_trick, hand_summary

            # Hand continues: Trick winner plays next
            self.state.current_turn_seat = winning_seat
            self._reset_deadline()
            return True, None, played, completed_trick, None

        # Trick continues: next counterclockwise seat
        self.state.current_turn_seat = (seat + 1) % 4
        self._reset_deadline()
        return True, None, played, None, None

    def get_public_state(self) -> Dict[str, Any]:
        """Returns safe public state visible to all players and spectators."""
        return {
            "game_mode": self.state.game_mode.value,
            "stage": self.state.stage.value,
            "hakem_seat": self.state.hakem_seat,
            "trump_suit": self.state.trump_suit,
            "current_turn_seat": self.state.current_turn_seat,
            "lead_suit": self.state.lead_suit,
            "current_trick": [
                {"seat": p.seat, "player_id": p.player_id, "card": p.card}
                for p in self.state.current_trick
            ],
            "team_a_match_points": self.state.team_a_match_points,
            "team_b_match_points": self.state.team_b_match_points,
            "hand_number": self.state.hand_number,
            "target_match_points": self.state.target_match_points,
            "action_deadline": self.state.action_deadline,
            "ceremony_cards": self.state.ceremony_cards,
            "match_winner_team": self.state.match_winner_team,
            "players": [
                {
                    "seat": p.seat,
                    "user_id": p.user_id,
                    "name": p.name,
                    "team": p.team,
                    "is_ai": p.is_ai,
                    "ai_difficulty": p.ai_difficulty,
                    "cards_remaining": len(p.cards),
                    "tricks_won": p.tricks_won,
                    "is_connected": p.is_connected,
                }
                for p in self.players
            ],
            "last_hand_summary": (
                {
                    "hand_number": self.last_hand_summary.hand_number,
                    "winner_team": self.last_hand_summary.winner_team,
                    "points_awarded": self.last_hand_summary.points_awarded,
                    "is_kot": self.last_hand_summary.is_kot,
                    "is_hakem_kot": self.last_hand_summary.is_hakem_kot,
                    "tricks_team_a": self.last_hand_summary.tricks_team_a,
                    "tricks_team_b": self.last_hand_summary.tricks_team_b,
                }
                if self.last_hand_summary
                else None
            ),
        }

    def get_private_state(self, seat: int) -> Dict[str, Any]:
        """Returns private player state (hand and legal moves). NEVER leaks other hands."""
        if not (0 <= seat < len(self.players)):
            return {"cards": [], "legal_moves": []}

        player = self.players[seat]
        legal_moves = (
            get_legal_moves(player.cards, self.state.current_trick)
            if self.state.current_turn_seat == seat and self.state.stage == HokmStage.PLAYING
            else []
        )
        return {
            "seat": seat,
            "cards": list(player.cards),
            "legal_moves": legal_moves,
        }

    def _reset_deadline(self) -> None:
        if self.state.turn_time_limit > 0:
            self.state.action_deadline = time.time() + self.state.turn_time_limit
        else:
            self.state.action_deadline = None

    def _sort_hand(self, player: PlayerSeat) -> None:
        """Sorts player's hand by suit and descending card rank."""
        suit_order = {Suit.SPADES: 0, Suit.HEARTS: 1, Suit.CLUBS: 2, Suit.DIAMONDS: 3}
        # If trump is known, put trump first
        if self.state.trump_suit:
            t = Suit(self.state.trump_suit)
            suit_order[t] = -1

        def sort_key(code: str):
            c = Card.from_code(code)
            return (suit_order.get(c.suit, 99), -c.value)

        player.cards.sort(key=sort_key)
