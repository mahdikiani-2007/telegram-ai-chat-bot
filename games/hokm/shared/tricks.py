from typing import List, Optional, Tuple
from games.hokm.shared.cards import Card, Suit
from games.hokm.shared.models import PlayedCard


def get_legal_moves(hand_codes: List[str], current_trick: List[PlayedCard]) -> List[str]:
    """Returns the subset of cards in hand that can legally be played."""
    if not hand_codes:
        return []
    if not current_trick:
        # First card of the trick: any card in hand is legal!
        return list(hand_codes)

    lead_card = Card.from_code(current_trick[0].card)
    lead_suit = lead_card.suit

    # Check if player has cards matching the led suit
    same_suit_cards = [c for c in hand_codes if Card.from_code(c).suit == lead_suit]
    if same_suit_cards:
        return same_suit_cards

    # Player has no cards of the led suit: any card can be played (ruffing or discarding)
    return list(hand_codes)


def validate_card_play(
    hand_codes: List[str],
    card_code: str,
    current_trick: List[PlayedCard],
) -> Tuple[bool, Optional[str]]:
    """Validates whether playing `card_code` is legal for the given hand and trick state."""
    if card_code not in hand_codes:
        return False, "این کارت در دست شما نیست."

    legal_moves = get_legal_moves(hand_codes, current_trick)
    if card_code not in legal_moves:
        lead_card = Card.from_code(current_trick[0].card)
        return False, f"شما باید از خال بازی شده ({lead_card.suit.persian_name}) کارت بیندازید."

    return True, None


def resolve_trick(
    trick_cards: List[PlayedCard],
    trump_suit_str: Optional[str],
) -> PlayedCard:
    """Calculates the winning PlayedCard for the trick using standard Hokm rules."""
    if not trick_cards:
        raise ValueError("Cannot resolve empty trick")

    lead_suit = Card.from_code(trick_cards[0].card).suit
    trump_suit = Suit(trump_suit_str) if trump_suit_str else None

    winning_play = trick_cards[0]
    winning_card = Card.from_code(winning_play.card)

    for candidate_play in trick_cards[1:]:
        candidate_card = Card.from_code(candidate_play.card)
        if candidate_card.beats(winning_card, lead_suit, trump_suit):
            winning_play = candidate_play
            winning_card = candidate_card

    return winning_play
