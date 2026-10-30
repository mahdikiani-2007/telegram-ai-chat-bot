import React, { useState } from 'react';
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

export const HokmTable: React.FC<HokmTableProps> = ({
  publicState,
  privateState,
  myUserId,
  onPlayCard,
  onSendChat,
}) => {
  const [selectedCard, setSelectedCard] = useState<string | null>(null);

  const players: any[] = publicState?.players || [];
  const mySeat = privateState?.seat ?? 0;
  const myCards: string[] = privateState?.cards || [];
  const legalMoves: string[] = privateState?.legal_moves || [];
  const currentTrick: any[] = publicState?.current_trick || [];
  const trumpSuit = publicState?.trump_suit;
  const currentTurnSeat = publicState?.current_turn_seat;
  const isMyTurn = currentTurnSeat === mySeat;
  const isFourPlayer = publicState?.game_mode === 'hokm_4p';
  const isThreePlayer = publicState?.game_mode === 'hokm_3p';

  // Order other players relative to mySeat
  const totalSeats = players.length || 4;
  const getRelativePlayer = (offset: number) => {
    const seatIdx = (mySeat + offset) % totalSeats;
    return players.find((p) => p.seat === seatIdx);
  };

  const handleCardClick = (cardCode: string) => {
    if (!isMyTurn) return;
    if (!legalMoves.includes(cardCode)) return;

    if (selectedCard === cardCode) {
      // Execute play
      sounds.playCardSlap();
      triggerHaptic('medium');
      onPlayCard(cardCode);
      setSelectedCard(null);
    } else {
      sounds.playCardSlap();
      triggerHaptic('light');
      setSelectedCard(cardCode);
    }
  };

  const handleCenterTableClick = () => {
    if (selectedCard && isMyTurn && legalMoves.includes(selectedCard)) {
      sounds.playCardSlap();
      triggerHaptic('medium');
      onPlayCard(selectedCard);
      setSelectedCard(null);
    }
  };

  // Helper to render a player pod
  const renderPlayerPod = (player: any) => {
    if (!player) return null;
    const isHakem = publicState?.hakem_seat === player.seat;
    const isActive = currentTurnSeat === player.seat;

    return (
      <div className={`player-pod ${isActive ? 'active' : ''}`}>
        <div className="avatar-ring">
          {player.is_ai ? '🤖' : '👤'}
          {isHakem && <span className="crown-badge">👑</span>}
        </div>
        <span className="player-name">{player.name}</span>
        <span className="player-tricks-count">
          دست: {player.tricks_won} ({player.cards_remaining} برگ)
        </span>
      </div>
    );
  };

  // Trick cards layout positions
  const getTrickCardTransform = (seat: number) => {
    const relOffset = (seat - mySeat + totalSeats) % totalSeats;
    if (totalSeats === 4) {
      if (relOffset === 0) return 'translateY(36px)'; // bottom (me)
      if (relOffset === 1) return 'translateX(40px)';  // right
      if (relOffset === 2) return 'translateY(-36px)'; // top (partner)
      return 'translateX(-40px)';                     // left
    } else if (totalSeats === 3) {
      if (relOffset === 0) return 'translateY(36px)';
      if (relOffset === 1) return 'translate(36px, -24px)';
      return 'translate(-36px, -24px)';
    } else {
      // 2-player
      if (relOffset === 0) return 'translateY(32px)';
      return 'translateY(-32px)';
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, width: '100%' }}>
      {/* Table Felt */}
      <div className="hokm-table">
        <div className="table-watermark">HOKM</div>

        {/* Trump Pill */}
        {trumpSuit && (
          <div className="trump-badge">
            <span>حکم:</span>
            <span style={{ color: 'var(--gold-light)' }}>{SUIT_NAMES[trumpSuit] || trumpSuit}</span>
          </div>
        )}

        {/* Match Points Banner */}
        <div className="score-banner">
          {isFourPlayer ? (
            <span>
              ما: <b style={{ color: 'var(--gold)' }}>{publicState?.team_a_match_points}</b> — آنها:{' '}
              <b style={{ color: '#93c5fd' }}>{publicState?.team_b_match_points}</b>
            </span>
          ) : (
            <span>دست {publicState?.hand_number || 1} (هدف: ۷)</span>
          )}
        </div>

        {/* TOP SEAT */}
        <div style={{ display: 'flex', justifyContent: 'center', width: '100%', marginTop: 28 }}>
          {totalSeats === 4 && renderPlayerPod(getRelativePlayer(2))}
          {totalSeats === 2 && renderPlayerPod(getRelativePlayer(1))}
          {totalSeats === 3 && (
            <div style={{ display: 'flex', justifyContent: 'space-around', width: '100%' }}>
              {renderPlayerPod(getRelativePlayer(1))}
              {renderPlayerPod(getRelativePlayer(2))}
            </div>
          )}
        </div>

        {/* MIDDLE ROW (for 4P: Left & Right seats + Center Trick) */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            position: 'relative',
            height: 140,
          }}
        >
          {totalSeats === 4 ? renderPlayerPod(getRelativePlayer(3)) : <div />}

          {/* Center Trick Pile */}
          <div className="trick-center" onClick={handleCenterTableClick}>
            {currentTrick.map((played: any, idx: number) => (
              <div
                key={`${played.seat}-${played.card}`}
                className="trick-card-slot"
                style={{
                  transform: getTrickCardTransform(played.seat),
                  zIndex: idx + 5,
                }}
              >
                <PlayingCard code={played.card} isPlayable={false} />
              </div>
            ))}
          </div>

          {totalSeats === 4 ? renderPlayerPod(getRelativePlayer(1)) : <div />}
        </div>

        {/* BOTTOM SEAT: My Pod & Action Status */}
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 12 }}>
          {renderPlayerPod(players.find((p) => p.seat === mySeat))}
          {isMyTurn && (
            <div
              style={{
                background: 'var(--gold-glow)',
                border: '1px solid var(--gold)',
                color: '#fef08a',
                padding: '4px 10px',
                borderRadius: 999,
                fontSize: 12,
                fontWeight: 800,
                animation: 'pulse 1s infinite alternate',
              }}
            >
              ⏳ نوبت شماست
            </div>
          )}
        </div>
      </div>

      {/* MY HAND CARDS FAN */}
      <div className="my-hand-container">
        <div className="my-hand-fan">
          {myCards.map((code) => {
            const isPlayable = isMyTurn ? legalMoves.includes(code) : true;
            const isSelected = selectedCard === code;
            return (
              <PlayingCard
                key={code}
                code={code}
                isPlayable={isPlayable}
                isSelected={isSelected}
                onClick={() => handleCardClick(code)}
              />
            );
          })}
        </div>
      </div>

      {/* QUICK REACTIONS BAR */}
      <div style={{ display: 'flex', justifyContent: 'center', gap: 8, padding: '4px 0' }}>
        {['👏', '🔥', '👑', 'ایول!', 'دمت گرم', 'خسته نباشی'].map((reaction) => (
          <button
            key={reaction}
            onClick={() => onSendChat(reaction)}
            style={{
              background: 'rgba(255,255,255,0.06)',
              border: '1px solid var(--panel-border)',
              borderRadius: 8,
              padding: '4px 8px',
              fontSize: 12,
              color: 'var(--text-main)',
              cursor: 'pointer',
            }}
          >
            {reaction}
          </button>
        ))}
      </div>
    </div>
  );
};
