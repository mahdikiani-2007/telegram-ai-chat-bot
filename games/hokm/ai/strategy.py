from typing import List, Optional
from games.hokm.shared.cards import Card, Rank, Suit
from games.hokm.shared.models import PlayedCard, Trick


def choose_trump_ai(hand_codes: List[str]) -> str:
    """Evaluates the 5 initial cards and chooses the best trump suit."""
    if not hand_codes:
        return Suit.SPADES.value

    suit_scores = {Suit.SPADES: 0, Suit.HEARTS: 0, Suit.DIAMONDS: 0, Suit.CLUBS: 0}
    for code in hand_codes:
        c = Card.from_code(code)
        # Quantity + rank value weight
        suit_scores[c.suit] += 10 + c.value

    best_suit = max(suit_scores.items(), key=lambda kv: kv[1])[0]
    return best_suit.value


def choose_draft_card_ai(offered_cards: List[str], trump_suit: Optional[str]) -> str:
    """Chooses the stronger card of 2 offered in 2-Player draft mode."""
    if not offered_cards:
        raise ValueError("No cards offered")
    if len(offered_cards) == 1:
        return offered_cards[0]

    c1 = Card.from_code(offered_cards[0])
    c2 = Card.from_code(offered_cards[1])

    t = Suit(trump_suit) if trump_suit else None
    # Trumps have high priority
    score1 = c1.value + (20 if c1.suit == t else 0)
    score2 = c2.value + (20 if c2.suit == t else 0)

    return offered_cards[0] if score1 >= score2 else offered_cards[1]


def choose_card_easy(legal_moves: List[str]) -> str:
    """Easy AI: Chooses the lowest legal card or simple move."""
    cards = [Card.from_code(c) for c in legal_moves]
    # Prefer lowest value
    lowest = min(cards, key=lambda c: c.value)
    return lowest.code


def choose_card_medium(
    legal_moves: List[str],
    current_trick: List[PlayedCard],
    trump_suit_str: Optional[str],
) -> str:
    """Medium AI: Understands trump, tries to win trick if cheap, otherwise dumps lowest."""
    cards = [Card.from_code(c) for c in legal_moves]
    trump = Suit(trump_suit_str) if trump_suit_str else None

    # If leading: play high non-trump (Ace/King) if available, or low card
    if not current_trick:
        aces = [c for c in cards if c.rank == Rank.ACE and c.suit != trump]
        if aces:
            return aces[0].code
        # Otherwise lead safe medium/low card
        non_trumps = [c for c in cards if c.suit != trump]
        if non_trumps:
            return min(non_trumps, key=lambda c: c.value).code
        return min(cards, key=lambda c: c.value).code

    # If following: check current winning card
    lead_suit = Card.from_code(current_trick[0].card).suit
    current_winner = current_trick[0]
    current_winning_card = Card.from_code(current_winner.card)

    for p in current_trick[1:]:
        pc = Card.from_code(p.card)
        if pc.beats(current_winning_card, lead_suit, trump):
            current_winner = p
            current_winning_card = pc

    # Winning candidate cards
    winning_cards = [c for c in cards if c.beats(current_winning_card, lead_suit, trump)]
    if winning_cards:
        # Win with the lowest possible winning card!
        best_win = min(winning_cards, key=lambda c: (1 if c.suit == trump else 0, c.value))
        return best_win.code

    # Cannot win: dump lowest non-trump, or lowest card
    non_trumps = [c for c in cards if c.suit != trump]
    if non_trumps:
        return min(non_trumps, key=lambda c: c.value).code
    return min(cards, key=lambda c: c.value).code


def choose_card_hard(
    legal_moves: List[str],
    current_trick: List[PlayedCard],
    completed_tricks: List[Trick],
    my_seat: int,
    trump_suit_str: Optional[str],
    is_team_mode: bool = False,
) -> str:
    """Hard AI:
    - Checks partner's winning status in 4P (avoids overtaking partner unless necessary).
    - If partner is winning, sluffs garbage (low card of an off-suit).
    - Counts Aces and high trumps.
    - If leading: leads Ace if safe, or pulls opponents' trumps if holding dominant trump hand.
    """
    cards = [Card.from_code(c) for c in legal_moves]
    trump = Suit(trump_suit_str) if trump_suit_str else None
    partner_seat = (my_seat + 2) % 4 if is_team_mode else None

    # On lead:
    if not current_trick:
        # Check if we have unbeatable Aces in non-trump suits
        aces = [c for c in cards if c.rank == Rank.ACE and c.suit != trump]
        if aces:
            return aces[0].code

        # If holding dominant trump (A or K of trump), lead trump to draw out enemy trumps
        top_trumps = [c for c in cards if c.suit == trump and c.value >= 13]
        if len([c for c in cards if c.suit == trump]) >= 4 and top_trumps:
            return top_trumps[0].code

        # Otherwise lead low from longest non-trump suit
        non_trumps = [c for c in cards if c.suit != trump]
        if non_trumps:
            return min(non_trumps, key=lambda c: c.value).code
        return min(cards, key=lambda c: c.value).code

    # Following lead:
    lead_suit = Card.from_code(current_trick[0].card).suit
    current_winner = current_trick[0]
    current_winning_card = Card.from_code(current_winner.card)

    for p in current_trick[1:]:
        pc = Card.from_code(p.card)
        if pc.beats(current_winning_card, lead_suit, trump):
            current_winner = p
            current_winning_card = pc

    # In 4P, if partner currently holds the winning card, DO NOT overtake!
    if is_team_mode and current_winner.seat == partner_seat:
        # Discard the lowest possible card (sluff)
        non_trumps = [c for c in cards if c.suit != trump]
        if non_trumps:
            return min(non_trumps, key=lambda c: c.value).code
        return min(cards, key=lambda c: c.value).code

    # Opponent is winning: try to beat them
    winning_cards = [c for c in cards if c.beats(current_winning_card, lead_suit, trump)]
    if winning_cards:
        # Prefer winning with non-trump first, else lowest trump
        non_trump_wins = [c for c in winning_cards if c.suit != trump]
        if non_trump_wins:
            return min(non_trump_wins, key=lambda c: c.value).code
        return min(winning_cards, key=lambda c: c.value).code

    # Cannot win: dump lowest value card to preserve future power
    non_trumps = [c for c in cards if c.suit != trump]
    if non_trumps:
        return min(non_trumps, key=lambda c: c.value).code
    return min(cards, key=lambda c: c.value).code


def get_ai_action_decision(
    difficulty: str,
    legal_moves: List[str],
    current_trick: List[PlayedCard],
    completed_tricks: List[Trick],
    my_seat: int,
    trump_suit_str: Optional[str],
    is_team_mode: bool = False,
) -> str:
    """Routes to the requested AI difficulty."""
    diff = (difficulty or "medium").lower()
    if diff == "easy":
        return choose_card_easy(legal_moves)
    elif diff == "hard":
        return choose_card_hard(
            legal_moves, current_trick, completed_tricks, my_seat, trump_suit_str, is_team_mode
        )
    return choose_card_medium(legal_moves, current_trick, trump_suit_str)
