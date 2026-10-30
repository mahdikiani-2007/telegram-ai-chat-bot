from enum import Enum
from typing import Optional


class Suit(str, Enum):
    SPADES = "S"      # پیک ♠
    HEARTS = "H"      # دل ♥
    DIAMONDS = "D"    # خشت ♦
    CLUBS = "C"       # گشنیز ♣

    @property
    def symbol(self) -> str:
        return {
            Suit.SPADES: "♠",
            Suit.HEARTS: "♥",
            Suit.DIAMONDS: "♦",
            Suit.CLUBS: "♣",
        }[self]

    @property
    def persian_name(self) -> str:
        return {
            Suit.SPADES: "پیک",
            Suit.HEARTS: "دل",
            Suit.DIAMONDS: "خشت",
            Suit.CLUBS: "گشنیز",
        }[self]

    @property
    def tie_break_order(self) -> int:
        # Spades > Hearts > Diamonds > Clubs
        return {
            Suit.SPADES: 4,
            Suit.HEARTS: 3,
            Suit.DIAMONDS: 2,
            Suit.CLUBS: 1,
        }[self]


class Rank(str, Enum):
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"
    TEN = "10"
    JACK = "J"
    QUEEN = "Q"
    KING = "K"
    ACE = "A"

    @property
    def order(self) -> int:
        orders = {
            Rank.TWO: 2,
            Rank.THREE: 3,
            Rank.FOUR: 4,
            Rank.FIVE: 5,
            Rank.SIX: 6,
            Rank.SEVEN: 7,
            Rank.EIGHT: 8,
            Rank.NINE: 9,
            Rank.TEN: 10,
            Rank.JACK: 11,
            Rank.QUEEN: 12,
            Rank.KING: 13,
            Rank.ACE: 14,
        }
        return orders[self]


class Card:
    __slots__ = ("suit", "rank", "code", "value")

    def __init__(self, rank: Rank, suit: Suit):
        self.rank = rank
        self.suit = suit
        self.code = f"{rank.value}{suit.value}"
        self.value = rank.order

    @classmethod
    def from_code(cls, code: str) -> "Card":
        code = code.strip().upper()
        if len(code) == 3:  # e.g. "10S"
            rank_str, suit_str = code[:2], code[2]
        elif len(code) == 2:  # e.g. "AS", "7D"
            rank_str, suit_str = code[0], code[1]
        else:
            raise ValueError(f"Invalid card code: {code}")

        suit = Suit(suit_str)
        rank_map = {
            "2": Rank.TWO,
            "3": Rank.THREE,
            "4": Rank.FOUR,
            "5": Rank.FIVE,
            "6": Rank.SIX,
            "7": Rank.SEVEN,
            "8": Rank.EIGHT,
            "9": Rank.NINE,
            "10": Rank.TEN,
            "J": Rank.JACK,
            "Q": Rank.QUEEN,
            "K": Rank.KING,
            "A": Rank.ACE,
        }
        rank = rank_map.get(rank_str)
        if not rank:
            raise ValueError(f"Invalid rank in code: {code}")
        card = cls(rank, suit)
        card.code = code
        return card

    def beats(self, other: "Card", led_suit: Suit, trump_suit: Optional[Suit]) -> bool:
        """Returns True if self wins against other given led_suit and trump_suit."""
        # Trump beats non-trump
        if trump_suit is not None:
            if self.suit == trump_suit and other.suit != trump_suit:
                return True
            if other.suit == trump_suit and self.suit != trump_suit:
                return False
            if self.suit == trump_suit and other.suit == trump_suit:
                return self.value > other.value

        # Neither or both are trump: must match led_suit to win
        if self.suit == led_suit and other.suit != led_suit:
            return True
        if other.suit == led_suit and self.suit != led_suit:
            return False
        if self.suit == led_suit and other.suit == led_suit:
            return self.value > other.value

        # Neither is led suit (and neither is trump)
        return False

    def ceremony_compare(self, other: "Card") -> int:
        """Compares two cards for Hakem ceremony: higher rank wins, tied rank uses suit priority.
        Returns > 0 if self wins, < 0 if other wins, 0 if equal.
        """
        if self.value != other.value:
            return self.value - other.value
        return self.suit.tie_break_order - other.suit.tie_break_order

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Card):
            return False
        return self.code == other.code

    def __hash__(self) -> int:
        return hash(self.code)

    def __repr__(self) -> str:
        return f"Card({self.code})"
