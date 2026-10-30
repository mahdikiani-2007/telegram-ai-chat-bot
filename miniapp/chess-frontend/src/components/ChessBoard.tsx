import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Chess, Square } from 'chess.js';
import { ChessPiece } from './ChessPieces';

interface ChessBoardProps {
  fen: string;
  myColor: 'white' | 'black' | null;
  isMyTurn: boolean;
  lastMove: string | null;
  check: boolean;
  onMove: (from: string, to: string, promotion?: string) => boolean;
  onPawnPromotionNeeded: (from: string, to: string) => void;
}

interface DragState {
  square: string;
  piece: { type: string; color: 'w' | 'b' };
  startX: number;
  startY: number;
  currentX: number;
  currentY: number;
  isDragging: boolean;
  squareSize: number;
}

const FILES = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'];
const RANKS = ['8', '7', '6', '5', '4', '3', '2', '1'];

export const ChessBoard: React.FC<ChessBoardProps> = ({
  fen,
  myColor,
  isMyTurn,
  lastMove,
  check,
  onMove,
  onPawnPromotionNeeded,
}) => {
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [dragState, setDragState] = useState<DragState | null>(null);
  const [hoverSquare, setHoverSquare] = useState<string | null>(null);

  const boardRef = useRef<HTMLDivElement>(null);
  const dragStateRef = useRef<DragState | null>(null);
  dragStateRef.current = dragState;

  const hoverSquareRef = useRef<string | null>(null);
  hoverSquareRef.current = hoverSquare;

  const chess = new Chess(fen);
  const isFlipped = myColor === 'black';

  // Compute displayed files and ranks depending on orientation
  const displayedFiles = isFlipped ? [...FILES].reverse() : FILES;
  const displayedRanks = isFlipped ? [...RANKS].reverse() : RANKS;

  // Compute legal moves for selected square
  const legalDestinations: string[] = [];
  const activeSquare = dragState?.square || selectedSquare;
  if (activeSquare) {
    try {
      const moves = chess.moves({ square: activeSquare as Square, verbose: true });
      moves.forEach((m) => legalDestinations.push(m.to));
    } catch {}
  }

  // Parse last move squares
  const lastFrom = lastMove ? lastMove.substring(0, 2) : null;
  const lastTo = lastMove ? lastMove.substring(2, 4) : null;

  // Locate current turn's king for check highlight
  let checkKingSquare: string | null = null;
  if (check) {
    const currentTurn = chess.turn();
    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        const sq = `${FILES[f]}${RANKS[7 - r]}` as Square;
        const p = chess.get(sq);
        if (p && p.type === 'k' && p.color === currentTurn) {
          checkKingSquare = sq;
          break;
        }
      }
      if (checkKingSquare) break;
    }
  }

  const getSquareFromCoords = useCallback(
    (clientX: number, clientY: number): string | null => {
      // 1. Direct hit-test using elementFromPoint (always exact, regardless of RTL/LTR or layout)
      try {
        const el = document.elementFromPoint(clientX, clientY);
        const sqEl = el?.closest('[data-square]');
        const sq = sqEl?.getAttribute('data-square');
        if (sq) return sq;
      } catch {}

      // 2. Mathematical fallback using board bounding rect (strictly LTR)
      if (!boardRef.current) return null;
      const rect = boardRef.current.getBoundingClientRect();
      if (
        clientX < rect.left ||
        clientX > rect.right ||
        clientY < rect.top ||
        clientY > rect.bottom
      ) {
        return null;
      }
      const col = Math.floor(((clientX - rect.left) / rect.width) * 8);
      const row = Math.floor(((clientY - rect.top) / rect.height) * 8);
      if (col >= 0 && col < 8 && row >= 0 && row < 8) {
        return `${displayedFiles[col]}${displayedRanks[row]}`;
      }
      return null;
    },
    [displayedFiles, displayedRanks]
  );

  const canSelectPiece = useCallback(
    (piece: { type: string; color: string } | null) => {
      if (!piece) return false;
      if (!isMyTurn) return false;
      if (myColor === 'white' && piece.color !== 'w') return false;
      if (myColor === 'black' && piece.color !== 'b') return false;
      return true;
    },
    [isMyTurn, myColor]
  );

  const executeMove = useCallback(
    (fromSq: string, toSq: string) => {
      const movingPiece = chess.get(fromSq as Square);
      const isWhitePawn =
        movingPiece?.type === 'p' && movingPiece?.color === 'w' && toSq.endsWith('8');
      const isBlackPawn =
        movingPiece?.type === 'p' && movingPiece?.color === 'b' && toSq.endsWith('1');

      if (isWhitePawn || isBlackPawn) {
        onPawnPromotionNeeded(fromSq, toSq);
        setSelectedSquare(null);
        return;
      }

      onMove(fromSq, toSq);
      setSelectedSquare(null);
      try {
        (window as any).Telegram?.WebApp?.HapticFeedback?.impactOccurred('light');
      } catch {}
    },
    [chess, onMove, onPawnPromotionNeeded]
  );

  const handlePointerDown = (e: React.PointerEvent, sq: string) => {
    if (!isMyTurn) return;
    const piece = chess.get(sq as Square);

    // If a piece was already selected, clicking on a legal destination executes the move
    if (selectedSquare && legalDestinations.includes(sq)) {
      executeMove(selectedSquare, sq);
      return;
    }

    // Only allow starting drag on own pieces
    if (piece && canSelectPiece(piece)) {
      const boardRect = boardRef.current?.getBoundingClientRect();
      const sqSize = boardRect ? boardRect.width / 8 : 45;

      setSelectedSquare(sq);
      setDragState({
        square: sq,
        piece: { type: piece.type, color: piece.color },
        startX: e.clientX,
        startY: e.clientY,
        currentX: e.clientX,
        currentY: e.clientY,
        isDragging: false,
        squareSize: sqSize,
      });
      setHoverSquare(sq);
    } else {
      setSelectedSquare(null);
    }
  };

  // Global window pointermove and pointerup listeners for fluid tracking outside board boundary
  useEffect(() => {
    const handlePointerMove = (e: PointerEvent) => {
      const currentDrag = dragStateRef.current;
      if (!currentDrag) return;

      const dist = Math.hypot(e.clientX - currentDrag.startX, e.clientY - currentDrag.startY);
      const isDraggingNow = currentDrag.isDragging || dist > 6;

      const targetSq = getSquareFromCoords(e.clientX, e.clientY);
      setHoverSquare(targetSq);

      setDragState((prev) => {
        if (!prev) return null;
        return {
          ...prev,
          currentX: e.clientX,
          currentY: e.clientY,
          isDragging: isDraggingNow,
        };
      });
    };

    const handlePointerUp = (e: PointerEvent) => {
      const currentDrag = dragStateRef.current;
      if (!currentDrag) return;

      if (currentDrag.isDragging) {
        const targetSq = getSquareFromCoords(e.clientX, e.clientY) || hoverSquareRef.current;
        if (targetSq && targetSq !== currentDrag.square) {
          // Check if legal move
          const moves = chess.moves({ square: currentDrag.square as Square, verbose: true });
          const isLegal = moves.some((m) => m.to === targetSq);
          if (isLegal) {
            executeMove(currentDrag.square, targetSq);
          }
        }
      }

      setDragState(null);
      setHoverSquare(null);
    };

    const handlePointerCancel = () => {
      setDragState(null);
      setHoverSquare(null);
    };

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
    window.addEventListener('pointercancel', handlePointerCancel);

    return () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.removeEventListener('pointercancel', handlePointerCancel);
    };
  }, [fen, isMyTurn, myColor, getSquareFromCoords, executeMove, chess]);

  return (
    <div className="chess-board-container" ref={boardRef} style={{ touchAction: 'none' }}>
      <div className="chess-board">
        {displayedRanks.map((rank, rIdx) =>
          displayedFiles.map((file, fIdx) => {
            const sq = `${file}${rank}`;
            const isLight = (rIdx + fIdx) % 2 === 0;
            const piece = chess.get(sq as Square);
            const isSelected = selectedSquare === sq || dragState?.square === sq;
            const isLastMove = lastFrom === sq || lastTo === sq;
            const isCheckSquare = checkKingSquare === sq;
            const isLegal = legalDestinations.includes(sq);
            const isHovered = dragState?.isDragging && hoverSquare === sq && isLegal;
            const isBeingDragged = dragState?.isDragging && dragState.square === sq;

            // Coordinates labels (bottom edge and left edge)
            const showFile = rIdx === 7;
            const showRank = fIdx === 0;

            const isDraggablePiece = isMyTurn && canSelectPiece(piece);

            return (
              <div
                key={sq}
                data-square={sq}
                className={`square ${isLight ? 'light' : 'dark'} ${isSelected ? 'selected' : ''} ${
                  isLastMove ? 'last-move' : ''
                } ${isCheckSquare ? 'check' : ''} ${isHovered ? 'drag-over' : ''}`}
                onPointerDown={(e) => handlePointerDown(e, sq)}
              >
                {/* Board edge coordinates */}
                {showFile && <span className="coordinate file">{file}</span>}
                {showRank && <span className="coordinate rank">{rank}</span>}

                {/* Legal destination indicator */}
                {isLegal && !piece && <div className="legal-dot" />}
                {isLegal && piece && <div className="legal-capture-ring" />}

                {/* Chess piece */}
                {piece && (
                  <div
                    className={`chess-piece ${isDraggablePiece ? 'draggable' : ''} ${
                      isBeingDragged ? 'dragging-ghost' : ''
                    }`}
                  >
                    <ChessPiece type={piece.type} color={piece.color} />
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Floating piece while dragging */}
      {dragState?.isDragging && (
        <div
          className="floating-drag-piece"
          style={{
            position: 'fixed',
            left: `${dragState.currentX - dragState.squareSize / 2}px`,
            top: `${dragState.currentY - dragState.squareSize / 2}px`,
            width: `${dragState.squareSize}px`,
            height: `${dragState.squareSize}px`,
            pointerEvents: 'none',
            zIndex: 9999,
          }}
        >
          <ChessPiece type={dragState.piece.type} color={dragState.piece.color} />
        </div>
      )}
    </div>
  );
};
