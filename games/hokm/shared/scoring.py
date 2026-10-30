from typing import Dict, List, Optional
from games.hokm.shared.models import GameMode, HandSummary, PlayerSeat


def evaluate_hand_result(
    game_mode: GameMode,
    hakem_seat: int,
    trump_suit: str,
    hand_number: int,
    players: List[PlayerSeat],
) -> Optional[HandSummary]:
    """Evaluates whether the hand is finished and returns HandSummary if finished."""
    if game_mode == GameMode.FOUR_PLAYER:
        # Team A (seats 0 & 2) vs Team B (seats 1 & 3)
        tricks_team_a = players[0].tricks_won + players[2].tricks_won
        tricks_team_b = players[1].tricks_won + players[3].tricks_won

        hakem_team = 0 if hakem_seat in (0, 2) else 1

        # Strict majority of 13 tricks is 7
        if tricks_team_a >= 7:
            winner_team = 0
            loser_tricks = tricks_team_b
        elif tricks_team_b >= 7:
            winner_team = 1
            loser_tricks = tricks_team_a
        else:
            return None  # Hand still in progress

        is_hakem_kot = (winner_team != hakem_team) and (loser_tricks == 0)
        is_kot = (not is_hakem_kot) and (loser_tricks == 0)

        points = 3 if is_hakem_kot else (2 if is_kot else 1)

        return HandSummary(
            hand_number=hand_number,
            hakem_seat=hakem_seat,
            trump_suit=trump_suit,
            tricks_team_a=tricks_team_a,
            tricks_team_b=tricks_team_b,
            tricks_by_seat={p.seat: p.tricks_won for p in players},
            winner_team=winner_team,
            points_awarded=points,
            is_kot=is_kot,
            is_hakem_kot=is_hakem_kot,
        )

    elif game_mode == GameMode.THREE_PLAYER:
        # 17 total tricks. Immediate end if someone reaches 9 tricks.
        total_tricks = sum(p.tricks_won for p in players)
        winner_seat: Optional[int] = None
        for p in players:
            if p.tricks_won >= 9:
                winner_seat = p.seat
                break

        # If nobody reached 9 yet, check if all 17 tricks were completed
        if winner_seat is None:
            if total_tricks < 17:
                return None  # Hand still in progress

            # All 17 tricks played: find maximum tricks won
            max_tricks = max(p.tricks_won for p in players)
            leaders = [p for p in players if p.tricks_won == max_tricks]
            if len(leaders) > 1:
                # Drawn hand
                return HandSummary(
                    hand_number=hand_number,
                    hakem_seat=hakem_seat,
                    trump_suit=trump_suit,
                    tricks_by_seat={p.seat: p.tricks_won for p in players},
                    winner_seat=None,
                    points_awarded=0,
                    is_draw=True,
                )
            winner_seat = leaders[0].seat

        # Check Kot / Hakem Kot for 3P
        hakem_tricks = next(p.tricks_won for p in players if p.seat == hakem_seat)
        is_hakem_kot = False
        is_kot = False

        if winner_seat == hakem_seat:
            # Hakem wins: Kot if all opponents have 0 tricks
            opponents_tricks = [p.tricks_won for p in players if p.seat != hakem_seat]
            if all(t == 0 for t in opponents_tricks):
                is_kot = True
        else:
            # Non-hakem wins: Hakem Kot if Hakem has 0 tricks
            if hakem_tricks == 0:
                is_hakem_kot = True

        points = 3 if is_hakem_kot else (2 if is_kot else 1)

        return HandSummary(
            hand_number=hand_number,
            hakem_seat=hakem_seat,
            trump_suit=trump_suit,
            tricks_by_seat={p.seat: p.tricks_won for p in players},
            winner_seat=winner_seat,
            points_awarded=points,
            is_kot=is_kot,
            is_hakem_kot=is_hakem_kot,
        )

    elif game_mode == GameMode.TWO_PLAYER:
        # 13 tricks. First to 7 tricks wins immediately.
        winner_seat: Optional[int] = None
        for p in players:
            if p.tricks_won >= 7:
                winner_seat = p.seat
                break

        if winner_seat is None:
            return None  # In progress

        opponent_seat = (winner_seat + 1) % 2
        opponent_tricks = players[opponent_seat].tricks_won

        is_hakem_kot = (winner_seat != hakem_seat) and (opponent_tricks == 0)
        is_kot = (not is_hakem_kot) and (opponent_tricks == 0)
        points = 3 if is_hakem_kot else (2 if is_kot else 1)

        return HandSummary(
            hand_number=hand_number,
            hakem_seat=hakem_seat,
            trump_suit=trump_suit,
            tricks_by_seat={p.seat: p.tricks_won for p in players},
            winner_seat=winner_seat,
            points_awarded=points,
            is_kot=is_kot,
            is_hakem_kot=is_hakem_kot,
        )

    return None


def calculate_next_hakem(
    game_mode: GameMode,
    current_hakem_seat: int,
    hand_summary: HandSummary,
) -> int:
    """Calculates the seat of the Hakem for the subsequent hand."""
    if game_mode == GameMode.FOUR_PLAYER:
        hakem_team = 0 if current_hakem_seat in (0, 2) else 1
        if hand_summary.winner_team == hakem_team:
            # Hakem's team won: keep same Hakem
            return current_hakem_seat
        else:
            # Hakem's team lost: rotate counterclockwise (next seat)
            return (current_hakem_seat + 1) % 4

    elif game_mode == GameMode.THREE_PLAYER:
        if hand_summary.is_draw:
            return current_hakem_seat
        if hand_summary.winner_seat == current_hakem_seat:
            return current_hakem_seat
        else:
            return (current_hakem_seat + 1) % 3

    elif game_mode == GameMode.TWO_PLAYER:
        if hand_summary.winner_seat == current_hakem_seat:
            return current_hakem_seat
        else:
            return (current_hakem_seat + 1) % 2

    return current_hakem_seat
