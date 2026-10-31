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


class TwoPlayerHokmEngine:
    """Authoritative game engine for 2-player Hokm with interactive two-card selection draft."""

    def __init__(self, room_id: str, turn_time_limit: int = 30):
        self.room_id = room_id
        self.players: List[PlayerSeat] = []
        self.state = HokmGameState(
            game_mode=GameMode.TWO_PLAYER,
            turn_time_limit=turn_time_limit,
        )
        self.last_hand_summary: Optional[HandSummary] = None

    def initialize_players(self, player_configs: List[Dict[str, Any]]) -> None:
        self.players = []
        for i, cfg in enumerate(player_configs[:2]):
            self.players.append(
                PlayerSeat(
                    seat=i,
                    user_id=cfg["user_id"],
                    name=cfg.get("name", f"Player {i+1}"),
                    team=None,
                    is_ai=cfg.get("is_ai", False),
                    ai_difficulty=cfg.get("ai_difficulty"),
                    cards=[],
                    tricks_won=0,
                    match_score=0,
                    is_connected=True,
                )
            )

    def start_ceremony(self) -> Dict[int, str]:
        """Performs initial Hakem selection ceremony by drawing 1 card each."""
        deck = create_standard_52_deck()
        shuffled, seed, commitment = secure_shuffle(deck)
        self.state.shuffle_commitment = commitment
        self.state.shuffle_seed = seed

        drawn_cards = {0: shuffled[0], 1: shuffled[1]}
        drawn_codes = {0: shuffled[0].code, 1: shuffled[1].code}

        if drawn_cards[0].ceremony_compare(drawn_cards[1]) >= 0:
            best_seat = 0
        else:
            best_seat = 1

        self.state.ceremony_cards = drawn_codes
        self.state.hakem_seat = best_seat
        self.state.stage = HokmStage.CEREMONY
        return drawn_codes

    def start_new_hand(self) -> None:
        """Deals 5 initial cards each and awaits Hakem's trump selection."""
        if self.state.hakem_seat is None:
            raise ValueError("Cannot start hand without Hakem")

        for p in self.players:
            p.cards = []
            p.tricks_won = 0

        self.state.trump_suit = None
        self.state.current_trick = []
        self.state.completed_tricks = []
        self.state.lead_suit = None
        self.state.discard_cards = []
        self.state.draft_turn = 0
        self.state.draft_offered_cards = None

        deck = create_standard_52_deck()
        shuffled, seed, commitment = secure_shuffle(deck)
        self.state.shuffle_commitment = commitment
        self.state.shuffle_seed = seed

        # Deal 5 initial cards each starting with Hakem (10 cards total)
        hakem = self.state.hakem_seat
        opp = (hakem + 1) % 2
        self.players[hakem].cards = [c.code for c in shuffled[0:5]]
        self.players[opp].cards = [c.code for c in shuffled[5:10]]

        # Remaining 42 cards in stock
        self.state.stock_cards = [c.code for c in shuffled[10:]]

        self.state.stage = HokmStage.TRUMP_SELECTION
        self.state.current_turn_seat = self.state.hakem_seat
        self._reset_deadline()

    def choose_trump(self, seat: int, suit_str: str) -> bool:
        """Hakem chooses trump; initiates the 16-turn two-card selection draft."""
        if self.state.stage != HokmStage.TRUMP_SELECTION:
            return False
        if seat != self.state.hakem_seat:
            return False

        try:
            suit = Suit(suit_str.upper())
        except ValueError:
            return False

        self.state.trump_suit = suit.value
        self.state.stage = HokmStage.TWO_PLAYER_DRAFT
        self.state.draft_turn = 0
        self.state.current_turn_seat = self.state.hakem_seat

        # Offer first 2 cards to Hakem
        self._prepare_next_draft_offer()
        return True

    def select_draft_card(self, seat: int, chosen_card: str) -> bool:
        """Active draft player selects 1 of the 2 offered cards.
        The selected card is kept, the unselected card goes to the hidden discard pile.
        """
        if self.state.stage != HokmStage.TWO_PLAYER_DRAFT:
            return False
        if seat != self.state.current_turn_seat:
            return False
        if not self.state.draft_offered_cards or chosen_card not in self.state.draft_offered_cards:
            return False

        player = self.players[seat]
        # Add chosen card to player's hand
        player.cards.append(chosen_card)

        # Move discarded card to hidden discard pile
        offered = list(self.state.draft_offered_cards)
        offered.remove(chosen_card)
        discarded_card = offered[0]
        self.state.discard_cards.append(discarded_card)
        self.state.draft_offered_cards = None

        self.state.draft_turn += 1

        # Check if 16 selection turns completed (8 for Hakem, 8 for opponent)
        if self.state.draft_turn >= 16:
            # Both players now have exactly 5 + 8 = 13 cards!
            for p in self.players:
                self._sort_hand(p)

            self.state.stage = HokmStage.PLAYING
            self.state.current_turn_seat = self.state.hakem_seat
            self._reset_deadline()
            return True

        # Alternate turn to opponent
        self.state.current_turn_seat = (self.state.hakem_seat + self.state.draft_turn) % 2
        self._prepare_next_draft_offer()
        return True

    def _prepare_next_draft_offer(self) -> None:
        """Pops 2 cards from top of stock to present privately to active player."""
        if len(self.state.stock_cards) < 2:
            return
        card1 = self.state.stock_cards.pop(0)
        card2 = self.state.stock_cards.pop(0)
        self.state.draft_offered_cards = [card1, card2]
        self._reset_deadline()

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

        player.cards.remove(card_code)
        played = PlayedCard(seat=seat, player_id=player.user_id, card=card_code)
        self.state.current_trick.append(played)

        if len(self.state.current_trick) == 1:
            self.state.lead_suit = Card.from_code(card_code).suit.value

        # Trick is complete when both players played (2 cards)
        if len(self.state.current_trick) == 2:
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

            # Check if hand ended (first to 7 tricks)
            hand_summary = evaluate_hand_result(
                game_mode=GameMode.TWO_PLAYER,
                hakem_seat=self.state.hakem_seat,
                trump_suit=self.state.trump_suit,
                hand_number=self.state.hand_number,
                players=self.players,
            )

            if hand_summary:
                self.last_hand_summary = hand_summary
                winner_player = self.players[hand_summary.winner_seat]
                winner_player.match_score += hand_summary.points_awarded

                if winner_player.match_score >= self.state.target_match_points:
                    self.state.stage = HokmStage.MATCH_ENDED
                    self.state.match_winner_seat = winner_player.seat
                else:
                    self.state.stage = HokmStage.HAND_ENDED
                    self.state.hakem_seat = calculate_next_hakem(
                        GameMode.TWO_PLAYER, self.state.hakem_seat, hand_summary
                    )
                    self.state.hand_number += 1

                self.state.action_deadline = None
                return True, None, played, completed_trick, hand_summary

            self.state.current_turn_seat = winning_seat
            self._reset_deadline()
            return True, None, played, completed_trick, None

        self.state.current_turn_seat = (seat + 1) % 2
        self._reset_deadline()
        return True, None, played, None, None

    def get_public_state(self) -> Dict[str, Any]:
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
            "last_completed_trick": (
                {
                    "lead_seat": self.state.completed_tricks[-1].lead_seat,
                    "lead_card": self.state.completed_tricks[-1].lead_card,
                    "winner_seat": self.state.completed_tricks[-1].winner_seat,
                    "winner_id": self.state.completed_tricks[-1].winner_id,
                    "cards": [
                        {"seat": p.seat, "player_id": p.player_id, "card": p.card}
                        for p in self.state.completed_tricks[-1].cards
                    ],
                }
                if self.state.completed_tricks
                else None
            ),
            "hand_number": self.state.hand_number,
            "target_match_points": self.state.target_match_points,
            "action_deadline": self.state.action_deadline,
            "ceremony_cards": self.state.ceremony_cards,
            "match_winner_seat": self.state.match_winner_seat,
            "draft_turn": self.state.draft_turn,
            "players": [
                {
                    "seat": p.seat,
                    "user_id": p.user_id,
                    "name": p.name,
                    "team": None,
                    "is_ai": p.is_ai,
                    "ai_difficulty": p.ai_difficulty,
                    "cards_remaining": len(p.cards),
                    "tricks_won": p.tricks_won,
                    "match_score": p.match_score,
                    "is_connected": p.is_connected,
                }
                for p in self.players
            ],
            "last_hand_summary": (
                {
                    "hand_number": self.last_hand_summary.hand_number,
                    "winner_seat": self.last_hand_summary.winner_seat,
                    "points_awarded": self.last_hand_summary.points_awarded,
                    "is_kot": self.last_hand_summary.is_kot,
                    "is_hakem_kot": self.last_hand_summary.is_hakem_kot,
                    "tricks_by_seat": self.last_hand_summary.tricks_by_seat,
                }
                if self.last_hand_summary
                else None
            ),
        }

    def get_private_state(self, seat: int) -> Dict[str, Any]:
        if not (0 <= seat < len(self.players)):
            return {"cards": [], "legal_moves": [], "draft_offered_cards": None}

        player = self.players[seat]
        legal_moves = (
            get_legal_moves(player.cards, self.state.current_trick)
            if self.state.current_turn_seat == seat and self.state.stage == HokmStage.PLAYING
            else []
        )
        # Offer cards are ONLY visible to the active draft seat!
        offered = (
            list(self.state.draft_offered_cards)
            if self.state.stage == HokmStage.TWO_PLAYER_DRAFT
            and self.state.current_turn_seat == seat
            and self.state.draft_offered_cards
            else None
        )
        return {
            "seat": seat,
            "cards": list(player.cards),
            "legal_moves": legal_moves,
            "draft_offered_cards": offered,
        }

    def _reset_deadline(self) -> None:
        if self.state.turn_time_limit > 0:
            self.state.action_deadline = time.time() + self.state.turn_time_limit
        else:
            self.state.action_deadline = None

    def _sort_hand(self, player: PlayerSeat) -> None:
        suit_order = {Suit.SPADES: 0, Suit.HEARTS: 1, Suit.CLUBS: 2, Suit.DIAMONDS: 3}
        if self.state.trump_suit:
            suit_order[Suit(self.state.trump_suit)] = -1

        def sort_key(code: str):
            c = Card.from_code(code)
            return (suit_order.get(c.suit, 99), -c.value)

        player.cards.sort(key=sort_key)
