import React, { useEffect, useRef, useState } from 'react';
import { HokmLobby } from './components/HokmLobby';
import { HokmTable } from './components/HokmTable';
import {
  CeremonyModal,
  HandSummaryModal,
  MatchOverModal,
  TrumpSelectModal,
  TwoPlayerDraftModal,
} from './components/Modals';
import './styles/hokm.css';

type AuthContext = { initData: string; devUser?: number };

export const App: React.FC = () => {
  const [user, setUser] = useState<any>(null);
  const [ratings, setRatings] = useState<any>({});
  const [publicState, setPublicState] = useState<any>(null);
  const [privateState, setPrivateState] = useState<any>(null);
  const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');
  const [rematchMessage, setRematchMessage] = useState('');

  const wsRef = useRef<WebSocket | null>(null);
  const authRef = useRef<AuthContext>({ initData: '' });
  const shouldReconnectRef = useRef(true);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const sequenceRef = useRef(-1);
  const roomIdRef = useRef<string | null>(null);
  const statusTimerRef = useRef<number | null>(null);
  const connectInFlightRef = useRef(false);
  const connectionGenerationRef = useRef(0);

  const setActiveRoom = (roomId: string | null) => {
    roomIdRef.current = roomId;
    setActiveRoomId(roomId);
  };

  const showStatus = (message: string) => {
    setStatusMessage(message);
    if (statusTimerRef.current !== null) {
      window.clearTimeout(statusTimerRef.current);
    }
    statusTimerRef.current = window.setTimeout(() => {
      setStatusMessage('');
      statusTimerRef.current = null;
    }, 5000);
  };

  useEffect(() => {
    let disposed = false;
    const generation = ++connectionGenerationRef.current;
    const isCurrentGeneration = () => !disposed && connectionGenerationRef.current === generation;
    shouldReconnectRef.current = true;

    const params = new URLSearchParams(window.location.search);
    const devUserParam = params.get('user') || params.get('dev_user');
    const tg = (window as any).Telegram?.WebApp;
    if (tg) {
      try {
        tg.ready();
        tg.expand();
        tg.setHeaderColor?.('#03140d');
        tg.setBackgroundColor?.('#03140d');
      } catch {}
    }

    const auth: AuthContext = {
      initData: tg?.initData || '',
      devUser: devUserParam
        ? Number.parseInt(devUserParam, 10)
        : (!tg?.initData ? 1 : undefined),
    };
    authRef.current = auth;

    const fetchTicketAndConnect = async (): Promise<void> => {
      if (!isCurrentGeneration() || !shouldReconnectRef.current || connectInFlightRef.current) return;
      const existing = wsRef.current;
      if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
        return;
      }

      connectInFlightRef.current = true;
      try {
        const response = await fetch('/api/hokm/ticket', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(authRef.current),
        });
        const ticketData = await response.json();
        if (!response.ok || !ticketData.ok || !ticketData.ticket) {
          throw new Error(ticketData.error || 'دریافت مجوز اتصال ناموفق بود.');
        }
        if (!isCurrentGeneration() || !shouldReconnectRef.current) return;

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = new WebSocket(
          `${protocol}//${window.location.host}/ws/hokm?ticket=${encodeURIComponent(ticketData.ticket)}`
        );
        wsRef.current = socket;

        socket.onopen = () => {
          if (!isCurrentGeneration() || wsRef.current !== socket) return;
          reconnectAttemptRef.current = 0;
          setIsConnected(true);
          setStatusMessage('');
        };

        socket.onmessage = (event) => {
          if (disposed || wsRef.current !== socket) return;
          try {
            const message = JSON.parse(event.data);
            if (message.type === 'hokm.game.state') {
              const incomingRoomId = String(message.room_id || '');
              const incomingSeq = Number(message.seq ?? message.public?.seq ?? -1);

              if (roomIdRef.current !== incomingRoomId) {
                sequenceRef.current = -1;
                setActiveRoom(incomingRoomId || null);
              }
              if (incomingSeq >= 0 && incomingSeq < sequenceRef.current) return;
              if (incomingSeq >= 0) sequenceRef.current = incomingSeq;

              setPublicState({
                ...message.public,
                seq: incomingSeq >= 0 ? incomingSeq : message.public?.seq,
              });
              setPrivateState(message.private || null);
              setActiveRoom(incomingRoomId || null);
              setRematchMessage('');
              return;
            }

            if (message.type === 'hokm.room.created' || message.type === 'hokm.room.joined') {
              const incomingRoomId = String(message.room_id || '');
              if (incomingRoomId && roomIdRef.current !== incomingRoomId) {
                sequenceRef.current = -1;
                setActiveRoom(incomingRoomId);
                setPublicState(null);
                setPrivateState(null);
              }
              return;
            }

            if (message.type === 'hokm.room.left') {
              if (!message.room_id || message.room_id === roomIdRef.current) {
                sequenceRef.current = -1;
                setActiveRoom(null);
                setPublicState(null);
                setPrivateState(null);
                setRematchMessage('');
              }
              return;
            }

            if (message.type === 'hokm.match.rematch.pending') {
              setRematchMessage(message.message || 'در انتظار موافقت سایر بازیکنان...');
              return;
            }

            if (message.type === 'hokm.match.rematch.started') {
              const nextRoomId = String(message.room_id || '');
              if (nextRoomId) {
                sequenceRef.current = -1;
                setActiveRoom(nextRoomId);
                setPublicState(null);
                setPrivateState(null);
                setRematchMessage('');
              }
              return;
            }

            if (message.type === 'error') {
              showStatus(String(message.error || 'خطایی رخ داد.'));
            }
          } catch (error) {
            console.error('Hokm WebSocket message error:', error);
          }
        };

        socket.onclose = () => {
          if (wsRef.current !== socket) return;
          wsRef.current = null;
          setIsConnected(false);
          if (!isCurrentGeneration() || !shouldReconnectRef.current || reconnectTimerRef.current !== null) return;

          reconnectAttemptRef.current += 1;
          const delay = Math.min(1000 * (2 ** (reconnectAttemptRef.current - 1)), 15000);
          reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            void fetchTicketAndConnect();
          }, delay);
        };

        socket.onerror = () => {
          if (wsRef.current === socket) setIsConnected(false);
        };
      } catch (error) {
        if (isCurrentGeneration() && shouldReconnectRef.current && reconnectTimerRef.current === null) {
          showStatus(error instanceof Error ? error.message : 'اتصال به سرور حکم برقرار نشد.');
          reconnectAttemptRef.current += 1;
          const delay = Math.min(1000 * (2 ** (reconnectAttemptRef.current - 1)), 15000);
          reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            void fetchTicketAndConnect();
          }, delay);
        }
      } finally {
        if (connectionGenerationRef.current === generation) {
          connectInFlightRef.current = false;
        }
      }
    };

    const loadProfile = async () => {
      try {
        const response = await fetch('/api/hokm/profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(auth),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'دریافت پروفایل ناموفق بود.');
        if (!isCurrentGeneration()) return;
        setUser({ user_id: data.user_id, name: data.name });
        setRatings(data.ratings || {});
        void fetchTicketAndConnect();
      } catch (error) {
        if (isCurrentGeneration()) showStatus(error instanceof Error ? error.message : 'دریافت پروفایل ناموفق بود.');
      }
    };

    void loadProfile();

    return () => {
      disposed = true;
      if (connectionGenerationRef.current === generation) {
        connectionGenerationRef.current += 1;
        connectInFlightRef.current = false;
      }
      shouldReconnectRef.current = false;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (statusTimerRef.current !== null) {
        window.clearTimeout(statusTimerRef.current);
        statusTimerRef.current = null;
      }
      const socket = wsRef.current;
      wsRef.current = null;
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'app_unmounted');
    };
  }, []);

  const sendWs = (type: string, payload: any): boolean => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      showStatus('اتصال بازی برقرار نیست؛ چند لحظه دیگر دوباره امتحان کنید.');
      return false;
    }
    try {
      socket.send(JSON.stringify({ type, payload }));
      return true;
    } catch {
      showStatus('ارسال درخواست ناموفق بود.');
      return false;
    }
  };

  const handleCreateRoom = (mode: string, timeLimit: number, isRanked: boolean) => {
    sendWs('hokm.room.create', { mode, turn_time_limit: timeLimit, is_ranked: isRanked });
  };

  const handleStartSoloAi = (mode: string, difficulty: string, timeLimit: number) => {
    sendWs('hokm.solo_ai.start', { mode, difficulty, turn_time_limit: timeLimit });
  };

  const handleJoinRoom = (code: string) => {
    sendWs('hokm.room.join', { room_id_or_code: code.trim().toUpperCase() });
  };

  const handleAddAi = (difficulty: string) => {
    if (activeRoomId) sendWs('hokm.room.add_ai', { room_id: activeRoomId, difficulty });
  };

  const handleStartGame = () => {
    if (activeRoomId) sendWs('hokm.room.start', { room_id: activeRoomId });
  };

  const handleSelectTrump = (suit: string) => {
    if (activeRoomId) sendWs('hokm.trump.select', { room_id: activeRoomId, suit });
  };

  const handleSelectDraftCard = (card: string) => {
    if (activeRoomId) sendWs('hokm.card.select', { room_id: activeRoomId, card });
  };

  const handlePlayCard = (card: string) => {
    if (activeRoomId) sendWs('hokm.card.play', { room_id: activeRoomId, card });
  };

  const handleSendChat = (text: string) => {
    if (activeRoomId) sendWs('hokm.chat.message', { room_id: activeRoomId, text });
  };

  const handleLeaveRoom = () => {
    if (activeRoomId) {
      sendWs('hokm.room.leave', { room_id: activeRoomId });
    }
  };

  const handleRematch = () => {
    if (activeRoomId) {
      setRematchMessage('درخواست بازی مجدد ارسال شد؛ منتظر موافقت سایر بازیکنان...');
      sendWs('hokm.match.rematch', { room_id: activeRoomId });
    }
  };

  const isHakem = publicState?.hakem_seat === privateState?.seat;
  const isPlaying = Boolean(publicState && publicState.stage !== 'waiting');

  return (
    <div className="hokm-container">
      <div className="hokm-header">
        <div className="hokm-brand">
          <span>👑</span>
          <span>حکم آنلاین</span>
        </div>
        <div className={`connection-pill ${isConnected ? 'connected' : 'disconnected'}`}>
          <span className="connection-dot" />
          {isConnected ? 'متصل' : 'در حال اتصال…'}
        </div>
      </div>

      {statusMessage && <div className="hokm-status-message" role="status">⚠️ {statusMessage}</div>}

      {!isPlaying ? (
        <HokmLobby
          user={user}
          ratings={ratings}
          activeRoom={publicState}
          onCreateRoom={handleCreateRoom}
          onStartSoloAi={handleStartSoloAi}
          onJoinRoom={handleJoinRoom}
          onAddAi={handleAddAi}
          onStartGame={handleStartGame}
          onLeaveRoom={handleLeaveRoom}
        />
      ) : (
        <HokmTable
          publicState={publicState}
          privateState={privateState}
          myUserId={user?.user_id}
          onPlayCard={handlePlayCard}
          onSendChat={handleSendChat}
        />
      )}

      {publicState?.stage === 'ceremony' && (
        <CeremonyModal
          ceremonyCards={publicState.ceremony_cards || {}}
          hakemSeat={publicState.hakem_seat}
          players={publicState.players || []}
        />
      )}

      {publicState?.stage === 'trump_selection' && isHakem && (
        <TrumpSelectModal onSelectSuit={handleSelectTrump} />
      )}

      {publicState?.stage === 'two_player_draft' && privateState?.draft_offered_cards && (
        <TwoPlayerDraftModal
          offeredCards={privateState.draft_offered_cards}
          onSelectCard={handleSelectDraftCard}
        />
      )}

      {publicState?.stage === 'hand_ended' && publicState?.last_hand_summary && (
        <HandSummaryModal
          summary={publicState.last_hand_summary}
          gameMode={publicState.game_mode}
          players={publicState.players || []}
        />
      )}

      {publicState?.stage === 'match_ended' && (
        <MatchOverModal
          isWinner={
            publicState.match_winner_team !== undefined && publicState.match_winner_team !== null
              ? (publicState.players?.find((player: any) => player.seat === privateState?.seat)?.team === publicState.match_winner_team)
              : publicState.match_winner_seat === privateState?.seat
          }
          scoreText={
            publicState.game_mode === 'hokm_4p'
              ? `${publicState.team_a_match_points} - ${publicState.team_b_match_points}`
              : (publicState.players || []).map((player: any) => `${player.name}: ${player.match_score}`).join(' | ')
          }
          rematchVotes={publicState.rematch_votes?.length || 0}
          rematchRequired={publicState.rematch_required || 0}
          onRematch={handleRematch}
          onReturnLobby={handleLeaveRoom}
        />
      )}

      {rematchMessage && <div className="rematch-toast">{rematchMessage}</div>}
    </div>
  );
};
