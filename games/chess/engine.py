from typing import List, Optional, Tuple
import chess

from games.chess.models import GameEndReason, GameResult


class ChessEngine:
    """Authoritative server-side chess engine wrapping python-chess.
    Validates moves, tracks board state, and detects terminal conditions.
    """

    def __init__(self, fen: str = chess.STARTING_FEN):
        self._board = chess.Board(fen)

    @property
    def board(self) -> chess.Board:
        return self._board

    def get_fen(self) -> str:
        return self._board.fen()

    def get_turn(self) -> str:
        return "white" if self._board.turn == chess.WHITE else "black"

    def is_check(self) -> bool:
        return self._board.is_check()

    def is_checkmate(self) -> bool:
        return self._board.is_checkmate()

    def is_stalemate(self) -> bool:
        return self._board.is_stalemate()

    def is_insufficient_material(self) -> bool:
        return self._board.is_insufficient_material()

    def is_fivefold_repetition(self) -> bool:
        return self._board.is_fivefold_repetition()

    def is_seventy_five_moves(self) -> bool:
        return self._board.is_seventyfive_moves()

    def can_claim_draw(self) -> bool:
        return (
            self._board.can_claim_threefold_repetition()
            or self._board.can_claim_fifty_moves()
            or self.is_fivefold_repetition()
            or self.is_seventy_five_moves()
            or self.is_insufficient_material()
        )

    def is_game_over(self) -> bool:
        return self._board.is_game_over(claim_draw=True)

    def get_legal_moves(self) -> List[str]:
        return [m.uci() for m in self._board.legal_moves]

    def is_legal_move(self, uci: str) -> bool:
        try:
            move = chess.Move.from_uci(uci.strip())
            return move in self._board.legal_moves
        except (ValueError, AttributeError):
            return False

    def make_move(self, uci: str) -> Tuple[str, str]:
        """Apply a move in UCI format (e.g. 'e2e4', 'e7e8q').
        Returns (san, fen_after).
        Raises ValueError if move is illegal.
        """
        uci_clean = uci.strip()
        try:
            move = chess.Move.from_uci(uci_clean)
        except ValueError as err:
            raise ValueError(f"Invalid move format: {uci_clean}") from err

        if move not in self._board.legal_moves:
            # Check if this might be a pawn move needing promotion default
            from_sq = move.from_square
            to_sq = move.to_square
            piece = self._board.piece_at(from_sq)
            if piece and piece.piece_type == chess.PAWN:
                target_rank = chess.square_rank(to_sq)
                if (piece.color == chess.WHITE and target_rank == 7) or (
                    piece.color == chess.BLACK and target_rank == 0
                ):
                    # Client omitted promotion piece, default to Queen
                    if not move.promotion:
                        move = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)

        if move not in self._board.legal_moves:
            raise ValueError(f"Illegal move: {uci_clean}")

        san = self._board.san(move)
        self._board.push(move)
        return san, self._board.fen()

    def get_result(self) -> Optional[Tuple[GameResult, GameEndReason]]:
        """Evaluate terminal condition according to authoritative chess rules."""
        if self._board.is_checkmate():
            # Winner is the side that just moved (opposite of current turn)
            if self._board.turn == chess.BLACK:
                return GameResult.WHITE_WINS, GameEndReason.CHECKMATE
            else:
                return GameResult.BLACK_WINS, GameEndReason.CHECKMATE

        if self._board.is_stalemate():
            return GameResult.DRAW, GameEndReason.STALEMATE

        if self._board.is_insufficient_material():
            return GameResult.DRAW, GameEndReason.INSUFFICIENT_MATERIAL

        if self._board.is_fivefold_repetition():
            return GameResult.DRAW, GameEndReason.FIVEFOLD_REPETITION

        if self.is_seventy_five_moves():
            return GameResult.DRAW, GameEndReason.SEVENTY_FIVE_MOVES

        if self._board.can_claim_threefold_repetition():
            return GameResult.DRAW, GameEndReason.THREEFOLD_REPETITION

        if self._board.can_claim_fifty_moves():
            return GameResult.DRAW, GameEndReason.FIFTY_MOVES

        return None
