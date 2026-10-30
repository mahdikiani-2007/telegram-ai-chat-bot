from typing import List
from games.hokm.shared.cards import Card, Rank, Suit


def create_standard_52_deck() -> List[Card]:
    """Creates a standard full 52-card deck."""
    deck: List[Card] = []
    for suit in (Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS, Suit.CLUBS):
        for rank in (
            Rank.TWO,
            Rank.THREE,
            Rank.FOUR,
            Rank.FIVE,
            Rank.SIX,
            Rank.SEVEN,
            Rank.EIGHT,
            Rank.NINE,
            Rank.TEN,
            Rank.JACK,
            Rank.QUEEN,
            Rank.KING,
            Rank.ACE,
        ):
            deck.append(Card(rank, suit))
    return deck


def create_3p_51_deck() -> List[Card]:
    """Creates a 51-card deck for 3-player Hokm (excludes 2 of Clubs / 2♣)."""
    full_deck = create_standard_52_deck()
    return [c for c in full_deck if c.code != "2C"]
