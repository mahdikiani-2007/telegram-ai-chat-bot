import React, { useEffect, useState, useRef } from 'react';
import { HokmLobby } from './components/HokmLobby';
import { HokmTable } from './components/HokmTable';
import { CeremonyModal, TrumpSelectModal, TwoPlayerDraftModal, MatchOverModal } from './components/Modals';
import './styles/hokm.css';

export const App: React.FC = () => {
  const [user, setUser] = useState<any>(null);
  const [ratings, setRatings] = useState<any>({});
  const [publicState, setPublicState] = useState<any>(null);
  const [privateState, setPrivateState] = useState<any>(null);
  const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<string>('');

  const wsRef = useRef<WebSocket | null>(null);

  // Parse Telegram WebApp or dev_user fallback
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const devUserParam = params.get('user');
    const tg = (window as any).Telegram?.WebApp;
    if (tg) {
      tg.ready();
      tg.expand();
    }

    const initData = tg?.initData || '';
    const devUser = devUserParam ? parseInt(devUserParam, 10) : (!initData ? 1 : undefined);

    // Fetch initial profile & ticket
    fetch('/api/hokm/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData, dev_user: devUser }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.ok) {
          setUser({ user_id: data.user_id, name: data.name });
          setRatings(data.ratings);
          // Connect WebSocket with ticket
          fetchTicketAndConnect(initData, devUser);
        }
      })
      .catch((err) => console.error('Profile fetch failed', err));
  }, []);

  const fetchTicketAndConnect = (initData: string, devUser?: number) => {
    fetch('/api/hokm/ticket', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData, dev_user: devUser }),
    })
      .then((r) => r.json())
      .then((res) => {
        if (res.ok && res.ticket) {
          connectWebSocket(res.ticket);
        }
      });
  };

  const connectWebSocket = (ticket: string) => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/hokm?ticket=${ticket}`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      setIsConnected(true);
      setStatusMessage('');
    };

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === 'hokm.game.state') {
          setPublicState(msg.public);
          setPrivateState(msg.private);
          setActiveRoomId(msg.room_id);
        } else if (msg.type === 'hokm.room.created' || msg.type === 'hokm.room.joined') {
          setActiveRoomId(msg.room_id);
        } else if (msg.type === 'error') {
          setStatusMessage(msg.error || 'خطایی رخ داد.');
          setTimeout(() => setStatusMessage(''), 4000);
        }
      } catch (err) {
        console.error('WS parse error', err);
      }
    };

    ws.onclose = () => {
      setIsConnected(false);
      // Reconnect after 3s
      setTimeout(() => {
        const params = new URLSearchParams(window.location.search);
        const devUser = params.get('user') ? parseInt(params.get('user')!, 10) : undefined;
        fetchTicketAndConnect((window as any).Telegram?.WebApp?.initData || '', devUser);
      }, 3000);
    };

    wsRef.current = ws;
  };

  const sendWs = (type: string, payload: any) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type, payload }));
    }
  };

  const handleCreateRoom = (mode: string, timeLimit: number, isRanked: boolean) => {
    sendWs('hokm.room.create', { mode, turn_time_limit: timeLimit, is_ranked: isRanked });
  };

  const handleStartSoloAi = (mode: string, difficulty: string, timeLimit: number) => {
    sendWs('hokm.solo_ai.start', { mode, difficulty, turn_time_limit: timeLimit });
  };

  const handleJoinRoom = (code: string) => {
    sendWs('hokm.room.join', { room_id_or_code: code });
  };

  const handleAddAi = (difficulty: string) => {
    sendWs('hokm.room.add_ai', { room_id: activeRoomId, difficulty });
  };

  const handleStartGame = () => {
    sendWs('hokm.room.start', { room_id: activeRoomId });
  };

  const handleSelectTrump = (suit: string) => {
    sendWs('hokm.trump.select', { room_id: activeRoomId, suit });
  };

  const handleSelectDraftCard = (card: string) => {
    sendWs('hokm.card.select', { room_id: activeRoomId, card });
  };

  const handlePlayCard = (card: string) => {
    sendWs('hokm.card.play', { room_id: activeRoomId, card });
  };

  const handleSendChat = (text: string) => {
    sendWs('hokm.chat.message', { room_id: activeRoomId, text });
  };

  const isHakem = publicState?.hakem_seat === privateState?.seat;
  const isPlaying = publicState && publicState.stage !== 'waiting';

  return (
    <div className="hokm-container">
      {/* Top Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 22 }}>👑</span>
          <span style={{ fontWeight: 900, fontSize: 18, color: 'var(--gold)' }}>حکم آنلاین</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-dim)' }}>
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: isConnected ? 'var(--success)' : 'var(--danger)',
              display: 'inline-block',
            }}
          />
          <span>{isConnected ? 'متصل' : 'درحال اتصال...'}</span>
        </div>
      </div>

      {statusMessage && (
        <div
          style={{
            background: 'rgba(239, 68, 68, 0.2)',
            border: '1px solid var(--danger)',
            color: '#fca5a5',
            padding: '8px 12px',
            borderRadius: 10,
            fontSize: 13,
            marginBottom: 10,
            textAlign: 'center',
          }}
        >
          ⚠️ {statusMessage}
        </div>
      )}

      {/* Main View: Lobby or Game Table */}
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

      {/* Modals */}
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

      {publicState?.stage === 'match_ended' && (
        <MatchOverModal
          isWinner={
            publicState.match_winner_team !== undefined
              ? (publicState.players?.find((p: any) => p.seat === privateState?.seat)?.team === publicState.match_winner_team)
              : (publicState.match_winner_seat === privateState?.seat)
          }
          scoreText={
            publicState.game_mode === 'hokm_4p'
              ? `${publicState.team_a_match_points} - ${publicState.team_b_match_points}`
              : `${publicState.players?.map((p: any) => `${p.name}: ${p.match_score}`).join(' | ')}`
          }
          onRematch={handleStartGame}
          onReturnLobby={() => {
            setPublicState(null);
            setActiveRoomId(null);
          }}
        />
      )}
    </div>
  );
};
