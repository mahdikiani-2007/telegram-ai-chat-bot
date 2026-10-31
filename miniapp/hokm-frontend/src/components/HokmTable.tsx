import React, { useEffect, useMemo, useRef, useState } from 'react';
import { PlayingCard } from './Cards';
import { triggerHaptic, sounds } from './AudioHaptics';

interface HokmTableProps {
  publicState: any;
  privateState: any;
  myUserId: number;
  onPlayCard: (card: string) => void;
  onSendChat: (text: string) => void;
}

const SUIT_NAMES: Record<string, string> = {
  S: 'پیک ♠',
  H: 'دل ♥',
  D: 'خشت ♦',
  C: 'گشنیز ♣',
};

const formatCard = (code: string) => {
  const rank = code.slice(0, -1);
  const suit = SUIT_NAMES[code.slice(-1)] || '';
  return `${rank} ${suit}`;
};

export const HokmTable: React.FC<HokmTableProps> = ({
  publicState,
  privateState,
  myUserId,
  onPlayCard,
  onSendChat,
}) => {
  const [selectedCard, setSelectedCard] = useState<string | null>(null);
  const [resolvedTrick, setResolvedTrick] = useState<any[]>([]);
  const [resolvingWinner, setResolvingWinner] = useState<number | null>(null);
  const lastCompletedKeyRef = useRef<string | null>(null);

  const players: any[] = publicState?.players || [];
  const mySeat = privateState?.seat ?? -1;
  const layoutSeat = mySeat >= 0 ? mySeat : 0;
  const myCards: string[] = privateState?.cards || [];
  const legalMoves: string[] = privateState?.legal_moves || [];
  const currentTrick: any[] = publicState?.current_trick || [];
  const lastCompleted = publicState?.last_completed_trick || null;
  const trumpSuit = publicState?.trump_suit;
  const currentTurnSeat = publicState?.current_turn_seat;
  const isMyTurn = currentTurnSeat === mySeat;
  const isFourPlayer = publicState?.game_mode === 'hokm_4p';
  const totalSeats = Math.max(2, Number(players.length) || (isFourPlayer ? 4 : 3));
  const myPlayer = players.find((player) => player?.seat === mySeat);
  const myTeam = myPlayer?.team;
  const turnDeadline = Number(publicState?.action_deadline || 0);
  const seq = Number(publicState?.seq || 0);

  const phaseLabel = useMemo(() => {
    const labels: Record<string, string> = {
      waiting: 'اتاق انتظار',
      ceremony: 'تعیین حاکم',
      trump_selection: 'انتخاب خال حکم',
      two_player_draft: 'انتخاب کارت',
      dealing: 'پخش کارت‌ها',
      playing: 'در حال بازی',
      hand_ended: 'پایان دست',
      match_ended: 'پایان مسابقه',
    };
    return labels[publicState?.stage] || 'در حال آماده‌سازی';
  }, [publicState?.stage]);

  useEffect(() => {
    setSelectedCard(null);
  }, [seq, currentTurnSeat, currentTrick.length, myCards.length]);

  useEffect(() => {
    if (!lastCompleted?.cards?.length) return;
    const key = `${publicState?.hand_number || 0}:${lastCompleted.cards.map((card: any) => `${card.seat}-${card.card}`).join('|')}`;
    if (lastCompletedKeyRef.current === key) return;
    lastCompletedKeyRef.current = key;
    setResolvedTrick(lastCompleted.cards);
    setResolvingWinner(lastCompleted.winner_seat ?? null);
    const timeout = window.setTimeout(() => {
      setResolvedTrick([]);
      setResolvingWinner(null);
    }, 1050);
    return () => window.clearTimeout(timeout);
  }, [seq, publicState?.hand_number, lastCompleted]);

  const displayedTrick = currentTrick.length > 0 ? currentTrick : resolvedTrick;
  const isResolvingTrick = currentTrick.length === 0 && resolvedTrick.length > 0;

  const getRelativePlayer = (offset: number) => {
    const seatIdx = (layoutSeat + offset) % totalSeats;
    return players.find((player) => player?.seat === seatIdx);
  };

  const handleCardClick = (cardCode: string) => {
    if (!isMyTurn || !legalMoves.includes(cardCode)) return;
    setSelectedCard((current) => current === cardCode ? null : cardCode);
    triggerHaptic('light');
  };

  const submitSelectedCard = () => {
    if (!selectedCard || !isMyTurn || !legalMoves.includes(selectedCard)) return;
    const cardToPlay = selectedCard;
    setSelectedCard(null);
    sounds.playCardSlap();
    triggerHaptic('medium');
    onPlayCard(cardToPlay);
  };

  const getTrickCardTransform = (seat: number) => {
    const relOffset = (seat - layoutSeat + totalSeats) % totalSeats;
    if (totalSeats === 4) {
      if (relOffset === 0) return 'translateY(36px)';
      if (relOffset === 1) return 'translateX(40px)';
      if (relOffset === 2) return 'translateY(-36px)';
      return 'translateX(-40px)';
    }
    if (totalSeats === 3) {
      if (relOffset === 0) return 'translateY(36px)';
      if (relOffset === 1) return 'translate(36px, -24px)';
      return 'translate(-36px, -24px)';
    }
    return relOffset === 0 ? 'translateY(32px)' : 'translateY(-32px)';
  };

  const renderPlayerPod = (player: any) => {
    if (!player) return null;
    const isHakem = publicState?.hakem_seat === player.seat;
    const isActive = currentTurnSeat === player.seat;
    const wonLastTrick = isResolvingTrick && resolvingWinner === player.seat;

    return (
      <div className={`player-pod ${isActive ? 'active' : ''} ${wonLastTrick ? 'won-trick' : ''}`} key={player.seat}>
        <div className="avatar-ring">
          {player.is_ai ? '🤖' : '👤'}
          {isHakem && <span className="crown-badge">👑</span>}
        </div>
        <span className="player-name">{player.name}</span>
        <span className="player-tricks-count">
          دست: {player.tricks_won ?? 0} ({player.cards_remaining ?? 0} برگ)
        </span>
        {wonLastTrick && <span className="trick-winner-label">برنده‌ی دور</span>}
      </div>
    );
  };

  const renderScore = () => {
    if (isFourPlayer) {
      const mine = myTeam === 1 ? publicState?.team_b_match_points : publicState?.team_a_match_points;
      const theirs = myTeam === 1 ? publicState?.team_a_match_points : publicState?.team_b_match_points;
      return (
        <span>
          تیم شما: <b className="my-team-score">{mine ?? 0}</b>
          <span className="score-separator"> — </span>
          حریف: <b className="opponent-team-score">{theirs ?? 0}</b>
        </span>
      );
    }
    return (
      <div className="individual-score-list">
        {players.map((player) => (
          <span key={player.seat} className={player.seat === mySeat ? 'individual-score mine' : 'individual-score'}>
            {player.name}: <b>{player.match_score ?? 0}</b>
          </span>
        ))}
      </div>
    );
  };

  return (
    <div className="hokm-game-layout">
      <div className="hokm-game-status">
        <div className="phase-chip">{phaseLabel}</div>
        {trumpSuit && <div className="trump-status-chip">حکم: <b>{SUIT_NAMES[trumpSuit] || trumpSuit}</b></div>}
        <div className="turn-status-chip">
          {isMyTurn ? 'نوبت شماست' : currentTurnSeat !== null && currentTurnSeat !== undefined
            ? `نوبت ${players.find((player) => player?.seat === currentTurnSeat)?.name || 'حریف'}`
            : 'منتظر حرکت'}
        </div>
        {turnDeadline > 0 && publicState?.stage !== 'hand_ended' && publicState?.stage !== 'match_ended' && (
          <div className="turn-deadline-chip">مهلت حرکت فعال است</div>
        )}
      </div>

      <div className="hokm-table">
        <div className="table-watermark" aria-hidden="true">HOKM</div>
        {trumpSuit && (
          <div className="trump-badge">
            <span>حکم:</span>
            <span style={{ color: 'var(--gold-light)' }}>{SUIT_NAMES[trumpSuit] || trumpSuit}</span>
          </div>
        )}
        <div className="score-banner">{renderScore()}</div>

        <div className="table-seat-top">
          {totalSeats === 4 && renderPlayerPod(getRelativePlayer(2))}
          {totalSeats === 2 && renderPlayerPod(getRelativePlayer(1))}
          {totalSeats === 3 && <>{renderPlayerPod(getRelativePlayer(1))}{renderPlayerPod(getRelativePlayer(2))}</>}
        </div>

        <div className={`table-middle-row ${totalSeats === 3 ? 'three-player' : ''}`}>
          {totalSeats === 4 ? renderPlayerPod(getRelativePlayer(3)) : <div className="side-seat-placeholder" />}
          <div className={`trick-center ${isResolvingTrick ? 'resolving' : ''}`}>
            {displayedTrick.map((played: any, idx: number) => (
              <div
                key={`${publicState?.hand_number || 0}-${played.seat}-${played.card}`}
                className="trick-card-slot"
                style={{ transform: getTrickCardTransform(played.seat), zIndex: idx + 5 }}
              >
                <div className="trick-card-motion">
                  <PlayingCard code={played.card} isPlayable={false} />
                </div>
              </div>
            ))}
          </div>
          {totalSeats === 4 ? renderPlayerPod(getRelativePlayer(1)) : <div className="side-seat-placeholder" />}
        </div>

        <div className="table-seat-bottom">
          {renderPlayerPod(myPlayer)}
          {isResolvingTrick && resolvingWinner !== null && (
            <div className="trick-resolution-chip">
              دور برای {players.find((player) => player?.seat === resolvingWinner)?.name || 'بازیکن'} شد
            </div>
          )}
        </div>
      </div>

      <div className="hand-section">
        <div className="hand-heading">
          <b>دست شما</b>
          <span>{myCards.length} کارت</span>
          {isMyTurn && <span className="hand-your-turn">نوبت شما</span>}
        </div>
        <div className={`my-hand-container ${!isMyTurn ? 'not-your-turn' : ''}`}>
          <div className="my-hand-fan">
            {myCards.map((code) => {
              const isPlayable = isMyTurn && legalMoves.includes(code);
              return (
                <PlayingCard
                  key={code}
                  code={code}
                  isPlayable={isPlayable}
                  isSelected={selectedCard === code}
                  onClick={() => handleCardClick(code)}
                />
              );
            })}
          </div>
        </div>
        {isMyTurn && selectedCard && (
          <div className="selected-card-action">
            <span>انتخاب‌شده: {formatCard(selectedCard)}</span>
            <button className="gold-btn play-selected-btn" onClick={submitSelectedCard}>بازی کردن کارت</button>
            <button className="clear-selection-btn" onClick={() => setSelectedCard(null)}>لغو انتخاب</button>
          </div>
        )}
        {!isMyTurn && publicState?.stage === 'playing' && (
          <div className="waiting-turn-note">کارت‌ها تا رسیدن نوبت شما غیرفعال هستند.</div>
        )}
      </div>

      <div className="quick-reactions">
        {['👏', '🔥', '👑', 'ایول!', 'دمت گرم', 'خسته نباشی'].map((reaction) => (
          <button key={reaction} onClick={() => onSendChat(reaction)}>{reaction}</button>
        ))}
      </div>
    </div>
  );
};
