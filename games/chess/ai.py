"""Small, bounded chess AI for single-player Mini App games.

The search is intentionally self-contained and CPU-bounded for the free hosting
profile. It uses iterative deepening, alpha-beta pruning, move ordering, and a
compact material/positional evaluation. The authoritative rules remain in
python-chess and the game service.
"""
from __future__ import annotations

import random
import time
from typing import List, Tuple

import chess


DIFFICULTY_DEPTHS = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
}

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 335,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

MATE_SCORE = 100_000
SEARCH_TIME_LIMIT_SECONDS = 0.65


class _SearchTimeout(Exception):
    pass


def _evaluate(board: chess.Board, perspective: chess.Color) -> int:
    """Evaluate the position from one color's perspective."""
    score = 0

    for square, piece in board.piece_map().items():
        sign = 1 if piece.color == perspective else -1
        value = PIECE_VALUES[piece.piece_type]
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        relative_rank = rank_index if piece.color == chess.WHITE else 7 - rank_index

        # Central squares are valuable for minor pieces and pawns.
        center_distance = abs(file_index - 3.5) + abs(rank_index - 3.5)
        center_bonus = int((7.0 - center_distance) * 4)

        positional = 0
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP):
            positional += center_bonus
        elif piece.piece_type == chess.PAWN:
            positional += relative_rank * 7 + center_bonus // 2
        elif piece.piece_type == chess.ROOK:
            positional += relative_rank * 2

        score += sign * (value + positional)

    # A small mobility bonus prevents passive positions without dominating
    # material or tactical considerations.
    current_turn = board.turn
    mobility = board.legal_moves.count()
    score += (2 if current_turn == perspective else -2) * mobility
    return score


def _terminal_score(board: chess.Board, perspective: chess.Color, ply: int) -> int | None:
    if board.is_checkmate():
        return -MATE_SCORE + ply if board.turn == perspective else MATE_SCORE - ply
    if (
        board.is_stalemate()
        or board.is_insufficient_material()
        or board.is_fivefold_repetition()
        or board.is_seventyfive_moves()
        or board.can_claim_threefold_repetition()
        or board.can_claim_fifty_moves()
    ):
        return 0
    return None


def _move_order_score(board: chess.Board, move: chess.Move) -> int:
    score = 0
    if board.is_capture(move):
        victim = board.piece_at(move.to_square)
        attacker = board.piece_at(move.from_square)
        victim_value = PIECE_VALUES.get(victim.piece_type, 0) if victim else 100
        attacker_value = PIECE_VALUES.get(attacker.piece_type, 0) if attacker else 0
        score += 10_000 + victim_value * 10 - attacker_value
    if move.promotion:
        score += 8_000 + PIECE_VALUES.get(move.promotion, 0)
    return score


def _ordered_moves(board: chess.Board) -> List[chess.Move]:
    moves = list(board.legal_moves)
    moves.sort(key=lambda move: _move_order_score(board, move), reverse=True)
    return moves


def _minimax(
    board: chess.Board,
    depth: int,
    alpha: int,
    beta: int,
    perspective: chess.Color,
    deadline: float,
    ply: int = 0,
) -> int:
    if time.monotonic() >= deadline:
        raise _SearchTimeout

    terminal = _terminal_score(board, perspective, ply)
    if terminal is not None:
        return terminal
    if depth <= 0:
        return _evaluate(board, perspective)

    maximizing = board.turn == perspective
    best = -MATE_SCORE if maximizing else MATE_SCORE

    for move in _ordered_moves(board):
        board.push(move)
        try:
            value = _minimax(
                board,
                depth - 1,
                alpha,
                beta,
                perspective,
                deadline,
                ply + 1,
            )
        finally:
            board.pop()

        if maximizing:
            best = max(best, value)
            alpha = max(alpha, best)
        else:
            best = min(best, value)
            beta = min(beta, best)

        if beta <= alpha:
            break

    return best


def _search_root(
    board: chess.Board,
    depth: int,
    perspective: chess.Color,
    deadline: float,
) -> Tuple[int, List[chess.Move]]:
    best_score = -MATE_SCORE
    best_moves: List[chess.Move] = []
    alpha = -MATE_SCORE
    beta = MATE_SCORE

    for move in _ordered_moves(board):
        if time.monotonic() >= deadline:
            raise _SearchTimeout

        board.push(move)
        try:
            score = _minimax(
                board,
                depth - 1,
                alpha,
                beta,
                perspective,
                deadline,
                1,
            )
        finally:
            board.pop()

        if score > best_score:
            best_score = score
            best_moves = [move]
        elif score == best_score:
            best_moves.append(move)

        alpha = max(alpha, best_score)

    return best_score, best_moves


def choose_move(fen: str, difficulty: str = "medium") -> str:
    """Return a legal UCI move for the side to move in *fen*."""
    board = chess.Board(fen)
    legal_moves = list(board.legal_moves)
    if not legal_moves:
        raise ValueError("No legal moves in the supplied position")

    level = difficulty if difficulty in DIFFICULTY_DEPTHS else "medium"
    target_depth = DIFFICULTY_DEPTHS[level]

    # Easy mode deliberately introduces variety while still choosing a legal
    # move. Medium/hard use bounded iterative deepening.
    if level == "easy":
        ordered = _ordered_moves(board)
        candidate_count = max(1, min(len(ordered), 1 + len(ordered) // 3))
        return random.choice(ordered[:candidate_count]).uci()

    perspective = board.turn
    deadline = time.monotonic() + SEARCH_TIME_LIMIT_SECONDS
    best_move = random.choice(legal_moves)

    for depth in range(1, target_depth + 1):
        try:
            _score, best_moves = _search_root(board, depth, perspective, deadline)
        except _SearchTimeout:
            break
        if best_moves:
            best_move = random.choice(best_moves)

    return best_move.uci()
