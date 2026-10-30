import React, { useEffect, useState } from 'react';
import { useTelegram } from './hooks/useTelegram';
import { useChessGame } from './hooks/useChessGame';
import { Screen } from './types';
import { ChessLobby } from './components/ChessLobby';
import { ChessRoom } from './components/ChessRoom';
import { ChessBoard } from './components/ChessBoard';
import { PlayerCard } from './components/PlayerCard';
import { GameControls } from './components/GameControls';
import { MoveHistory } from './components/MoveHistory';
import { PromotionModal, ResignConfirmModal, ResultModal } from './components/Modals';
import { joinRoom } from './services/api';
import { hapticSuccess, hapticTap } from './services/telegram';
import './styles/chess.css';

export const App: React.FC = () => {
  const { tg, user } = useTelegram();
  const urlParams = new URLSearchParams(window.location.search);
  const devParam = urlParams.get('user') || urlParams.get('dev_user') || '1';
  const currentUserId = user?.id || parseInt(devParam, 10) || 1;

  const [screen, setScreen] = useState<Screen>('lobby');
  const [roomId, setRoomId] = useState<string>('');
  const [inviteCode, setInviteCode] = useState<string>('');

  // Modals state
  const [showHistory, setShowHistory] = useState(false);
  const [showResignModal, setShowResignModal] = useState(false);
  const [showResultModal, setShowResultModal] = useState(false);
  const [gameResult, setGameResult] = useState<string | null>(null);
  const [gameReason, setGameReason] = useState<string | null>(null);

  // Promotion pending state
  const [promotionPending, setPromotionPending] = useState<{ from: string; to: string } | null>(null);

  // Core chess game hook
  const {
    fen,
    turn,
    check,
    lastMove,
    status,
    whiteRemaining,
    blackRemaining,
    moveHistory,
    whitePlayer,
    blackPlayer,
    myColor,
    isMyTurn,
    opponentConnected,
    drawOfferedBy,
    makeMove,
    resign,
    offerDraw,
    acceptDraw,
    declineDraw,
  } = useChessGame({
    roomId,
    currentUserId,
    onGameEnd: (res, reason) => {
      setGameResult(res);
      setGameReason(reason);
      setShowResultModal(true);
    },
  });

  // Deep-link check for ?invite=CODE on load
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const inviteParam = params.get('invite') || params.get('tgWebAppStartParam');
    if (inviteParam) {
      const code = inviteParam.startsWith('inv_') ? inviteParam.replace('inv_', '') : inviteParam;
      joinRoom(undefined, code)
        .then((res) => {
          setRoomId(res.room_id);
          setScreen('game');
          hapticSuccess();
        })
        .catch(() => {});
    }
  }, []);

  // Telegram BackButton integration
  useEffect(() => {
    if (!tg?.BackButton) return;

    if (screen === 'lobby') {
      tg.BackButton.hide();
    } else {
      tg.BackButton.show();
      const handleBack = () => {
        if (screen === 'waiting') {
          setScreen('lobby');
        } else if (screen === 'game') {
          if (status === 'active') {
            setShowResignModal(true);
          } else {
            setScreen('lobby');
          }
        }
      };
      tg.BackButton.onClick(handleBack);
      return () => {
        tg.BackButton.offClick(handleBack);
      };
    }
  }, [screen, tg, status]);

  // Player cards resolution (opponent at top, current player at bottom)
  const isPlayingBlack = myColor === 'black';

  const topPlayer = isPlayingBlack
    ? { name: whitePlayer?.name || 'سفید', color: 'white' as const, time: whiteRemaining }
    : { name: blackPlayer?.name || 'سیاه', color: 'black' as const, time: blackRemaining };

  const bottomPlayer = isPlayingBlack
    ? { name: blackPlayer?.name || user?.first_name || 'شما', color: 'black' as const, time: blackRemaining }
    : { name: whitePlayer?.name || user?.first_name || 'شما', color: 'white' as const, time: whiteRemaining };

  const isTopActiveTurn = isPlayingBlack ? turn === 'white' : turn === 'black';
  const isBottomActiveTurn = isPlayingBlack ? turn === 'black' : turn === 'white';

  return (
    <div className="app-container">
      {/* Top Brand Header */}
      <header className="top-header">
        <div className="brand-title">
          <span>♟️</span>
          <span>شطرنج L</span>
        </div>
        <div className="chip-status">
          <span>👤</span>
          <b>{user?.first_name || 'کاربر'}</b>
        </div>
      </header>

      {/* Screen 1: Lobby */}
      {screen === 'lobby' && (
        <ChessLobby
          activeRoomId={roomId && status === 'active' ? roomId : null}
          onGameCreated={(rId, code) => {
            setRoomId(rId);
            setInviteCode(code);
            setScreen('waiting');
          }}
          onGameJoined={(rId) => {
            setRoomId(rId);
            setScreen('game');
          }}
        />
      )}

      {/* Screen 2: Waiting Room */}
      {screen === 'waiting' && (
        <ChessRoom
          roomId={roomId}
          inviteCode={inviteCode}
          onGameStart={() => setScreen('game')}
          onCancel={() => setScreen('lobby')}
        />
      )}

      {/* Screen 3: Chess Match */}
      {screen === 'game' && (
        <main style={{ display: 'flex', flexDirection: 'column', flex: 1, justifyContent: 'space-between' }}>
          {(!myColor || status === 'waiting') ? (
            <div className="glass-panel" style={{ textAlign: 'center', padding: '32px 18px', marginTop: 24 }}>
              <div className="spinner" style={{ margin: '0 auto 14px' }} />
              <div style={{ fontWeight: 800, marginBottom: 6 }}>در حال همگام‌سازی بازی</div>
              <div style={{ color: 'var(--dim)', fontSize: 12, lineHeight: 1.8 }}>
                در حال دریافت رنگ مهره‌ها و وضعیت حریف از سرور...
              </div>
            </div>
          ) : (
            <>
              {/* Opponent Card (Top) */}
              <PlayerCard
                name={topPlayer.name}
                color={topPlayer.color}
                remainingMs={topPlayer.time}
                isActiveTurn={status === 'active' && isTopActiveTurn}
                isConnected={opponentConnected}
              />

              {/* Interactive Chess Board */}
              <ChessBoard
                fen={fen}
                myColor={myColor}
                isMyTurn={isMyTurn}
                lastMove={lastMove}
                check={check}
                onMove={(from, to) => makeMove(from, to, 'q')}
                onPawnPromotionNeeded={(from, to) => setPromotionPending({ from, to })}
              />

              {/* Player Card (Bottom) */}
              <PlayerCard
                name={bottomPlayer.name}
                color={bottomPlayer.color}
                remainingMs={bottomPlayer.time}
                isActiveTurn={status === 'active' && isBottomActiveTurn}
                isConnected={true}
              />

              {/* Action Controls */}
              <GameControls
                onResignClick={() => setShowResignModal(true)}
                onDrawClick={offerDraw}
                onToggleHistory={() => setShowHistory(true)}
                drawOfferedByMe={drawOfferedBy === currentUserId}
                isDrawIncoming={drawOfferedBy !== null && drawOfferedBy !== currentUserId}
                onAcceptDraw={acceptDraw}
                onDeclineDraw={declineDraw}
                isGameOver={status === 'finished'}
                onBackToLobby={() => {
                  setScreen('lobby');
                  setShowResultModal(false);
                }}
              />
            </>
          )}
        </main>
      )}

      {/* Modals */}
      {showHistory && (
        <MoveHistory moves={moveHistory} onClose={() => setShowHistory(false)} />
      )}

      <ResignConfirmModal
        isOpen={showResignModal}
        onConfirm={() => {
          setShowResignModal(false);
          resign();
          hapticTap('heavy');
        }}
        onCancel={() => setShowResignModal(false)}
      />

      <ResultModal
        isOpen={showResultModal}
        result={gameResult}
        reason={gameReason}
        myColor={myColor}
        onLobbyClick={() => {
          setShowResultModal(false);
          setScreen('lobby');
        }}
      />

      {promotionPending && (
        <PromotionModal
          color={myColor || 'white'}
          onSelect={(piece) => {
            makeMove(promotionPending.from, promotionPending.to, piece);
            setPromotionPending(null);
          }}
          onCancel={() => setPromotionPending(null)}
        />
      )}
    </div>
  );
};
