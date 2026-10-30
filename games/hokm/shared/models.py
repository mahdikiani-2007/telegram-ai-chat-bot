from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GameMode(str, Enum):
    TWO_PLAYER = "hokm_2p"
    THREE_PLAYER = "hokm_3p"
    FOUR_PLAYER = "hokm_4p"

    @property
    def player_count(self) -> int:
        return {
            GameMode.TWO_PLAYER: 2,
            GameMode.THREE_PLAYER: 3,
            GameMode.FOUR_PLAYER: 4,
        }[self]

    @property
    def cards_per_player(self) -> int:
        return {
            GameMode.TWO_PLAYER: 13,
            GameMode.THREE_PLAYER: 17,
            GameMode.FOUR_PLAYER: 13,
        }[self]

    @property
    def tricks_to_win_hand(self) -> int:
        # Hand winning threshold
        return {
            GameMode.TWO_PLAYER: 7,
            GameMode.THREE_PLAYER: 9,
            GameMode.FOUR_PLAYER: 7,
        }[self]


class HokmStage(str, Enum):
    WAITING = "waiting"                    # In lobby, waiting for seats to fill
    CEREMONY = "ceremony"                  # First Hakem card-drawing ceremony
    TRUMP_SELECTION = "trump_selection"    # Hakem has 5 cards and selects trump
    TWO_PLAYER_DRAFT = "two_player_draft"  # 2P only: 16 turns picking 1 card from 2
    DEALING_REMAINING = "dealing"          # Distributing remaining cards
    PLAYING = "playing"                    # Active trick-taking phase
    HAND_ENDED = "hand_ended"              # Hand finished, showing Kot / points, rotating Hakem
    MATCH_ENDED = "match_ended"            # Reached 7 match points!


@dataclass
class PlayerSeat:
    seat: int
    user_id: int
    name: str
    team: Optional[int] = None       # 0 = Team A, 1 = Team B (for 4P)
    is_ai: bool = False
    ai_difficulty: Optional[str] = None  # "easy", "medium", "hard"
    cards: List[str] = field(default_factory=list)  # Hidden private hand
    tricks_won: int = 0              # Tricks won in current hand
    match_score: int = 0             # Match points won (for individual 2P/3P)
    is_connected: bool = True
    disconnect_time: Optional[float] = None


@dataclass
class PlayedCard:
    seat: int
    player_id: int
    card: str


@dataclass
class Trick:
    lead_seat: int
    lead_card: str
    cards: List[PlayedCard] = field(default_factory=list)
    winner_seat: Optional[int] = None
    winner_id: Optional[int] = None


@dataclass
class HandSummary:
    hand_number: int
    hakem_seat: int
    trump_suit: str
    tricks_team_a: int = 0
    tricks_team_b: int = 0
    tricks_by_seat: Dict[int, int] = field(default_factory=dict)
    winner_team: Optional[int] = None
    winner_seat: Optional[int] = None
    points_awarded: int = 1
    is_kot: bool = False
    is_hakem_kot: bool = False
    is_draw: bool = False


@dataclass
class HokmGameState:
    game_mode: GameMode
    stage: HokmStage = HokmStage.WAITING
    hakem_seat: Optional[int] = None
    trump_suit: Optional[str] = None
    current_turn_seat: Optional[int] = None
    lead_suit: Optional[str] = None
    current_trick: List[PlayedCard] = field(default_factory=list)
    completed_tricks: List[Trick] = field(default_factory=list)
    team_a_match_points: int = 0   # Seats 0 & 2
    team_b_match_points: int = 0   # Seats 1 & 3
    target_match_points: int = 7
    hand_number: int = 1
    action_deadline: Optional[float] = None
    turn_time_limit: int = 30      # 10s, 15s, 20s, 30s, 45s, 60s, or 0 (unlimited)
    ceremony_cards: Dict[int, str] = field(default_factory=dict)
    draft_turn: int = 0            # 2P mode only: 0 to 15
    draft_offered_cards: Optional[List[str]] = None  # 2P mode only: 2 cards offered to active seat
    stock_cards: List[str] = field(default_factory=list)  # Remaining undealt deck
    discard_cards: List[str] = field(default_factory=list)  # 2P hidden discard pile
    shuffle_commitment: str = ""
    shuffle_seed: Optional[str] = None
    match_winner_seat: Optional[int] = None
    match_winner_team: Optional[int] = None
    match_start_time: float = 0.0
