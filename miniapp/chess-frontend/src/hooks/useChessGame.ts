import { useEffect, useRef, useState, useCallback } from 'react';
import { Chess } from 'chess.js';
import { GameState, MoveRecord, PlayerInfo } from '../types';
import { chessSocket } from '../services/websocket';
import { hapticError, hapticSuccess, hapticTap } from '../services/telegram';

interface UseChessGameProps {
  roomId: string;
  currentUserId: number;
  onGameEnd?: (result: string, reason: string) => void;
}

export function useChessGame({ roomId, currentUserId, onGameEnd }: UseChessGameProps) {
  const [chess] = useState(() => new Chess());
  const [fen, setFen] = useState(chess.fen());
  const [turn, setTurn] = useState<'white' | 'black'>('white');
  const [check, setCheck] = useState(false);
  const [lastMove, setLastMove] = useState<string | null>(null);
  const [status, setStatus] = useState<string>('waiting');
  const [seq, setSeq] = useState(0);

  const [whiteRemaining, setWhiteRemaining] = useState(300000);
  const [blackRemaining, setBlackRemaining] = useState(300000);
  const [moveHistory, setMoveHistory] = useState<MoveRecord[]>([]);

  const [whitePlayer, setWhitePlayer] = useState<PlayerInfo | null>(null);
  const [blackPlayer, setBlackPlayer] = useState<PlayerInfo | null>(null);
  const [opponentConnected, setOpponentConnected] = useState(true);

  const [drawOfferedBy, setDrawOfferedBy] = useState<number | null>(null);
  const [socketStatus, setSocketStatus] = useState<'connected' | 'connecting' | 'disconnected'>('disconnected');

  const seqRef = useRef(0);
  seqRef.current = seq;
  const authoritativeFenRef = useRef(chess.fen());
  const onGameEndRef = useRef(onGameEnd);
  onGameEndRef.current = onGameEnd;

  const clockIntervalRef = useRef<number | null>(null);

  // Determine player color
  const myColor: 'white' | 'black' | null =
    whitePlayer?.user_id === currentUserId
      ? 'white'
      : blackPlayer?.user_id === currentUserId
      ? 'black'
      : null;

  const isMyTurn = status === 'active' && myColor === turn;

  // Local clock countdown animation
  useEffect(() => {
    if (status !== 'active') {
      if (clockIntervalRef.current) clearInterval(clockIntervalRef.current);
      return;
    }

    clockIntervalRef.current = window.setInterval(() => {
      if (turn === 'white') {
        setWhiteRemaining((prev) => Math.max(0, prev - 100));
      } else {
        setBlackRemaining((prev) => Math.max(0, prev - 100));
      }
    }, 100);

    return () => {
      if (clockIntervalRef.current) clearInterval(clockIntervalRef.current);
    };
  }, [status, turn]);

  // Connect to WebSocket and attach message handler
  useEffect(() => {
    chessSocket.connect();
    const unsubStatus = chessSocket.onStatus(setSocketStatus);

    const unsubMsg = chessSocket.onMessage((msg) => {
      if ('game_id' in msg && msg.game_id !== roomId) return;

      switch (msg.type) {
        case 'game.start': {
          setWhitePlayer(msg.payload.white);
          setBlackPlayer(msg.payload.black);
          setStatus('active');
          chess.load(msg.payload.fen);
          authoritativeFenRef.current = msg.payload.fen;
          setFen(msg.payload.fen);
          setTurn('white');
          setCheck(false);
          setLastMove(null);
          setMoveHistory([]);
          setSeq(msg.seq);
          if (msg.payload.time_control) {
            setWhiteRemaining(msg.payload.time_control.initial_ms);
            setBlackRemaining(msg.payload.time_control.initial_ms);
          }
          hapticSuccess();
          break;
        }

        case 'game.state': {
          const payload = msg.payload;
          setSeq(msg.seq);
          chess.load(payload.fen);
          authoritativeFenRef.current = payload.fen;
          setFen(payload.fen);
          setTurn(payload.turn);
          setCheck(payload.check);
          setLastMove(payload.last_move);
          setStatus(payload.status);
          setWhiteRemaining(payload.white_ms);
          setBlackRemaining(payload.black_ms);
          if (payload.move_history) {
            setMoveHistory(payload.move_history);
          }
          hapticTap('medium');
          break;
        }

        case 'game.sync': {
          const p = msg.payload;
          setSeq(msg.seq);
          chess.load(p.fen);
          authoritativeFenRef.current = p.fen;
          setFen(p.fen);
          setTurn(p.turn);
          setCheck(p.check);
          setLastMove(p.last_move);
          setStatus(p.status);
          setWhiteRemaining(p.white_ms);
          setBlackRemaining(p.black_ms);
          if (p.move_history) setMoveHistory(p.move_history);
          if (p.white_id !== undefined && p.white_id !== null) {
            setWhitePlayer({ user_id: p.white_id, name: p.white_name || (p.white_id === p.player1_id ? p.player1_name : p.player2_name) || 'سفید' });
          }
          if (p.black_id !== undefined && p.black_id !== null) {
            setBlackPlayer({ user_id: p.black_id, name: p.black_name || (p.black_id === p.player1_id ? p.player1_name : p.player2_name) || 'سیاه' });
          }
          setDrawOfferedBy(p.draw_offered_by || null);
          break;
        }

        case 'game.clock': {
          setWhiteRemaining(msg.payload.white_ms);
          setBlackRemaining(msg.payload.black_ms);
          break;
        }

        case 'player.connected': {
          if (msg.payload.user_id !== currentUserId) {
            setOpponentConnected(true);
          }
          break;
        }

        case 'player.disconnected': {
          if (msg.payload.user_id !== currentUserId) {
            setOpponentConnected(false);
          }
          break;
        }

        case 'game.draw.offer': {
          setDrawOfferedBy(msg.payload.from_id);
          hapticTap('heavy');
          break;
        }

        case 'game.draw.declined': {
          setDrawOfferedBy(null);
          break;
        }

        case 'game.finished': {
          setStatus('finished');
          hapticTap('heavy');
          onGameEndRef.current?.(msg.payload.result, msg.payload.reason);
          break;
        }

        case 'error': {
          console.warn('Game error:', msg.payload.message);
          chess.load(authoritativeFenRef.current);
          setFen(authoritativeFenRef.current);
          hapticError();
          if (roomId) {
            chessSocket.send({ type: 'game.request_sync', game_id: roomId });
          }
          break;
        }
      }
    });

    // Do not request an empty room. For a real room, the socket service
    // queues this request if authentication/upgrade is still in progress.
    if (roomId) {
      chessSocket.send({
        type: 'game.request_sync',
        game_id: roomId,
      });
    }

    return () => {
      unsubStatus();
      unsubMsg();
    };
  }, [roomId, currentUserId, chess]);

  // Actions
  const makeMove = useCallback(
    (from: string, to: string, promotion: string = 'q'): boolean => {
      if (!isMyTurn || socketStatus !== 'connected') return false;

      try {
        // Validate against a temporary board. The rendered board remains
        // server-authoritative and changes only after game.state arrives.
        const preview = new Chess(fen);
        const move = preview.move({ from, to, promotion });
        if (!move) return false;

        const sent = chessSocket.send({
          type: 'game.move',
          game_id: roomId,
          move_id: `${Date.now()}_${Math.random().toString(36).substring(2, 9)}`,
          expected_seq: seqRef.current,
          payload: { uci: move.lan || `${from}${to}${promotion || ''}` },
        });

        if (!sent) return false;
        hapticTap('light');
        return true;
      } catch {
        hapticError();
        return false;
      }
    },
    [isMyTurn, socketStatus, fen, roomId]
  );

  const resign = useCallback(() => {
    chessSocket.send({
      type: 'game.resign',
      game_id: roomId,
    });
  }, [roomId]);

  const offerDraw = useCallback(() => {
    chessSocket.send({
      type: 'game.draw.offer',
      game_id: roomId,
    });
    setDrawOfferedBy(currentUserId);
  }, [roomId, currentUserId]);

  const acceptDraw = useCallback(() => {
    chessSocket.send({
      type: 'game.draw.accept',
      game_id: roomId,
    });
    setDrawOfferedBy(null);
  }, [roomId]);

  const declineDraw = useCallback(() => {
    chessSocket.send({
      type: 'game.draw.decline',
      game_id: roomId,
    });
    setDrawOfferedBy(null);
  }, [roomId]);

  const requestSync = useCallback(() => {
    chessSocket.send({
      type: 'game.request_sync',
      game_id: roomId,
    });
  }, [roomId]);

  return {
    chess,
    fen,
    turn,
    check,
    lastMove,
    status,
    seq,
    whiteRemaining,
    blackRemaining,
    moveHistory,
    whitePlayer,
    blackPlayer,
    myColor,
    isMyTurn,
    opponentConnected,
    drawOfferedBy,
    socketStatus,
    makeMove,
    resign,
    offerDraw,
    acceptDraw,
    declineDraw,
    requestSync,
  };
}
