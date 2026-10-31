import React, { useEffect, useMemo, useRef, useState } from 'react';
import { PlayingCard } from './Cards';
import { triggerHaptic, sounds } from './AudioHaptics';

interface HokmTableProps {
  publicState: any;
  privateState: any;
  myUserId: number;
  onPlayCard: (card: string) => void;
  onSendChat: (text: string) => void;
  chatMessages?: any[];
}

interface DragSession {
  code: string;
  pointerId: number;
  startX: number;
  startY: number;
  x: number;
  y: number;
  moved: boolean;
}

const SUIT_NAMES: Record<string, string> = {
  S: 'پیک ♠',
  H: 'دل ♥',
  D: 'خشت ♦',
  C: 'گشنیز ♣',
};
const QUICK_REACTIONS = ['👏', '🔥', '👑', 'ایول!', 'دمت گرم', 'خسته نباشی'];

const formatCard = (code: string) => {
  const rank = code.slice(0, -1);
  const suit = SUIT_NAMES[code.slice(-1)] || '';
  return rank + ' ' + suit;
};

const formatClock = (timestamp: number) => {
  if (!timestamp) return '';
  return new Date(timestamp * 1000).toLocaleTimeString('fa-IR', {
    hour: '2-digit',
    minute: '2-digit',
  });
};

export const HokmTable: React.FC<HokmTableProps> = ({
  publicState,
  privateState,
  myUserId,
  onPlayCard,
  onSendChat,
  chatMessages = [],
}) => {
  const [selectedCard, setSelectedCard] = useState<string | null>(null);
  const [resolvedTrick, setResolvedTrick] = useState<any[]>([]);
  const [resolvingWinner, setResolvingWinner] = useState<number | null>(null);
  const [chatOpen, setChatOpen] = useState(true);
  const [chatDraft, setChatDraft] = useState('');
  const [dragSession, setDragSession] = useState<DragSession | null>(null);
  const [dragHint, setDragHint] = useState('');
  const lastCompletedKeyRef = useRef<string | null>(null);
  const previousTrickLengthRef = useRef(0);
  const dragRef = useRef<DragSession | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);

  const players: any[] = publicState?.players || [];
  const mySeat = privateState?.seat ?? -1;
  const layoutSeat = mySeat >= 0 ? mySeat : 0;
  const myCards: string[] = privateState?.cards || [];
  const legalMoves: string[] = privateState?.legal_moves || [];
  const currentTrick: any[] = publicState?.current_trick || [];
  const lastCompleted = publicState?.last_completed_trick || null;
  const trumpSuit = publicState?.trump_suit;
  const currentTurnSeat = publicState?.current_turn_seat;
  const isFourPlayer = publicState?.game_mode === 'hokm_4p';
  const totalSeats = Math.max(2, Number(players.length) || (isFourPlayer ? 4 : 3));
  const myPlayer = players.find((player) => player?.seat === mySeat);
  const myTeam = myPlayer?.team;
  const turnDeadline = Number(publicState?.action_deadline || 0);
  const trickResolutionUntil = Number(publicState?.trick_resolution_until || 0);
  const seq = Number(publicState?.seq || 0);
  const isMotionLocked = now < trickResolutionUntil;
  const isMyTurn = currentTurnSeat === mySeat && !isMotionLocked;

  const phaseLabel = useMemo(() => {
    const labels: Record<string, string> = {
      waiting: 'اتاق انتظار',
      ceremony: 'مراسم تعیین حاکم',
      trump_selection: 'انتخاب خال حکم',
      two_player_draft: 'انتخاب کارت',
      dealing: 'پخش کارت‌ها',
      playing: 'در حال بازی',
      hand_ended: 'پایان دست',
      match_ended: 'پایان مسابقه',
    };
    return labels[publicState?.stage] || 'در حال آماده‌سازی';
  }, [publicState?.stage]);

  const teamTricks = (team: number) => players.reduce(
    (sum, player) => sum + (player?.team === team ? Number(player?.tricks_won || 0) : 0),
    0,
  );
  const teamATricks = teamTricks(0);
  const teamBTricks = teamTricks(1);
  const resolvingWinnerTeam = resolvingWinner === null
    ? null
    : players.find((player) => player?.seat === resolvingWinner)?.team;
  const displayedTeamATricks = Math.max(0, teamATricks - (resolvedTrick.length && resolvingWinnerTeam === 0 ? 1 : 0));
  const displayedTeamBTricks = Math.max(0, teamBTricks - (resolvedTrick.length && resolvingWinnerTeam === 1 ? 1 : 0));

  useEffect(() => {
    setSelectedCard(null);
    setDragHint('');
  }, [seq, currentTurnSeat, currentTrick.length, myCards.length]);

  useEffect(() => {
    const completedNow = previousTrickLengthRef.current > 0 && currentTrick.length === 0;
    previousTrickLengthRef.current = currentTrick.length;
    if (!completedNow || !lastCompleted?.cards?.length) return;

    const key = String(publicState?.hand_number || 0) + ':' + lastCompleted.cards
      .map((card: any) => String(card.seat) + '-' + card.card).join('|');
    if (lastCompletedKeyRef.current === key) return;
    lastCompletedKeyRef.current = key;
    setResolvedTrick(lastCompleted.cards);
    setResolvingWinner(lastCompleted.winner_seat ?? null);
    sounds.playTrickWin();
    triggerHaptic('success');
    const timeout = window.setTimeout(() => {
      setResolvedTrick([]);
      setResolvingWinner(null);
    }, 2320);
    return () => window.clearTimeout(timeout);
  }, [seq, currentTrick.length, lastCompleted, publicState?.hand_number]);

  useEffect(() => {
    if (!turnDeadline && !trickResolutionUntil) return;
    const interval = window.setInterval(() => setNow(Date.now() / 1000), 100);
    return () => window.clearInterval(interval);
  }, [turnDeadline, trickResolutionUntil]);

  useEffect(() => {
    if (!chatOpen || !chatScrollRef.current) return;
    chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
  }, [chatMessages.length, chatOpen]);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      const active = dragRef.current;
      if (!active || event.pointerId !== active.pointerId) return;
      event.preventDefault();
      const moved = active.moved || Math.hypot(event.clientX - active.startX, event.clientY - active.startY) > 9;
      const next = { ...active, x: event.clientX, y: event.clientY, moved };
      dragRef.current = next;
      setDragSession(next);
    };

    const onPointerUp = (event: PointerEvent) => {
      const active = dragRef.current;
      if (!active || event.pointerId !== active.pointerId) return;
      const moved = active.moved || Math.hypot(event.clientX - active.startX, event.clientY - active.startY) > 9;
      dragRef.current = null;
      setDragSession(null);

      if (!moved) {
        setDragHint('');
        if (isMyTurn && legalMoves.includes(active.code)) {
          setSelectedCard((current) => current === active.code ? null : active.code);
          triggerHaptic('light');
        }
        return;
      }

      const center = document.querySelector('.trick-center');
      const rect = center?.getBoundingClientRect();
      const padding = 54;
      const droppedOnTable = Boolean(
        rect &&
        event.clientX >= rect.left - padding &&
        event.clientX <= rect.right + padding &&
        event.clientY >= rect.top - padding &&
        event.clientY <= rect.bottom + padding
      );

      if (droppedOnTable && isMyTurn && legalMoves.includes(active.code)) {
        setSelectedCard(null);
        setDragHint('');
        sounds.playCardSlap();
        triggerHaptic('medium');
        onPlayCard(active.code);
      } else {
        setSelectedCard(active.code);
        setDragHint('برای انداختن کارت، آن را روی مرکز میز رها کن.');
        triggerHaptic('light');
      }
    };

    const onPointerCancel = (event: PointerEvent) => {
      if (!dragRef.current || event.pointerId !== dragRef.current.pointerId) return;
      dragRef.current = null;
      setDragSession(null);
    };

    window.addEventListener('pointermove', onPointerMove, { passive: false });
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerCancel);
    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('pointercancel', onPointerCancel);
    };
  }, [isMyTurn, legalMoves, onPlayCard]);

  const getRelativePlayer = (offset: number) => {
    const seatIdx = (layoutSeat + offset) % totalSeats;
    return players.find((player) => player?.seat === seatIdx);
  };

  const startCardDrag = (event: React.PointerEvent<HTMLDivElement>, cardCode: string) => {
    if (!isMyTurn || !legalMoves.includes(cardCode)) return;
    event.preventDefault();
    event.stopPropagation();
    const initial: DragSession = {
      code: cardCode,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      x: event.clientX,
      y: event.clientY,
      moved: false,
    };
    dragRef.current = initial;
    setDragSession(initial);
    setSelectedCard(cardCode);
    setDragHint('');
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
      if (relOffset === 0) return 'translateY(38px) rotate(-4deg)';
      if (relOffset === 1) return 'translateX(42px) rotate(4deg)';
      if (relOffset === 2) return 'translateY(-38px) rotate(4deg)';
      return 'translateX(-42px) rotate(-4deg)';
    }
    if (totalSeats === 3) {
      if (relOffset === 0) return 'translateY(34px)';
      if (relOffset === 1) return 'translate(38px, -25px)';
      return 'translate(-38px, -25px)';
    }
    return relOffset === 0 ? 'translateY(32px)' : 'translateY(-32px)';
  };

  const getCollectVector = (seat: number): [number, number] => {
    const relOffset = (seat - layoutSeat + totalSeats) % totalSeats;
    if (totalSeats === 4) {
      if (relOffset === 0) return [0, 145];
      if (relOffset === 1) return [165, 0];
      if (relOffset === 2) return [0, -145];
      return [-165, 0];
    }
    return relOffset === 1 ? [95, -110] : relOffset === 2 ? [-95, -110] : [0, 130];
  };

  const renderPlayerPod = (player: any) => {
    if (!player) return null;
    const isHakem = publicState?.hakem_seat === player.seat;
    const isActive = currentTurnSeat === player.seat;
    const wonLastTrick = resolvedTrick.length > 0 && resolvingWinner === player.seat;
    const remaining = Number(player.cards_remaining ?? 0);

    return (
      <div
        className={'player-pod royal-player-pod ' + (isActive ? 'active ' : '') +
          (wonLastTrick ? 'won-trick ' : '') + (player.team === myTeam && isFourPlayer ? 'friendly-pod' : '')}
        key={player.seat}
      >
        <div className="avatar-ring">
          {player.is_ai ? '🤖' : '👤'}
          {isHakem && <span className="crown-badge">♛</span>}
          <span className={'presence-dot ' + (player.is_connected === false ? 'offline' : '')} />
        </div>
        <span className="player-name">{player.name}</span>
        <span className="player-tricks-count">
          {isFourPlayer ? remaining + ' برگ' : 'دست: ' + (player.tricks_won ?? 0) + ' · ' + remaining + ' برگ'}
        </span>
        {isHakem && <span className="player-role-label">حاکم</span>}
        {wonLastTrick && <span className="trick-winner-label">دست برای این تیم</span>}
      </div>
    );
  };

  const renderScore = () => {
    if (isFourPlayer) {
      if (myTeam === 0 || myTeam === 1) {
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
        <span>
          تیم الف: <b className="my-team-score">{publicState?.team_a_match_points ?? 0}</b>
          <span className="score-separator"> — </span>
          تیم ب: <b className="opponent-team-score">{publicState?.team_b_match_points ?? 0}</b>
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

  const ownTricks = myTeam === 1 ? displayedTeamBTricks : displayedTeamATricks;
  const rivalTricks = myTeam === 1 ? displayedTeamATricks : displayedTeamBTricks;
  const myLabel = myTeam === null || myTeam === undefined ? 'تیم الف' : 'تیم شما';
  const rivalLabel = myTeam === null || myTeam === undefined ? 'تیم ب' : 'حریف';

  const renderCapturePile = (team: number, count: number, label: string, position: 'top' | 'bottom') => (
    <div
      className={'team-capture-pile ' + position + ' ' + (team === myTeam ? 'own-team' : 'rival-team') +
        (resolvedTrick.length && resolvingWinnerTeam === team ? ' pile-receiving' : '')}
      aria-label={label + ': ' + count + ' دست جمع‌شده'}
    >
      <div className="capture-card-stack">
        {Array.from({ length: Math.min(count, 7) }).map((_, index) => (
          <PlayingCard
            key={String(team) + '-' + String(index)}
            code="BACK"
            isPlayable={false}
            style={{ transform: 'translate(' + (index * 3) + 'px, ' + (-index * 2) + 'px) rotate(' + ((index - 3) * 3) + 'deg)' }}
          />
        ))}
        <span className="capture-count">{count}</span>
      </div>
      <span className="capture-label">{label}</span>
    </div>
  );

  const sendChat = (event: React.FormEvent) => {
    event.preventDefault();
    const text = chatDraft.trim().slice(0, 150);
    if (!text) return;
    onSendChat(text);
    setChatDraft('');
  };

  return (
    <div className="hokm-game-layout royal-game-layout">
      <div className="hokm-game-status">
        <div className="phase-chip">{phaseLabel}</div>
        {trumpSuit && <div className="trump-status-chip">حکم: <b>{SUIT_NAMES[trumpSuit] || trumpSuit}</b></div>}
        <div className={'turn-status-chip ' + (isMyTurn ? 'my-turn' : '')}>
          {isMotionLocked
            ? 'در حال جمع‌کردن دست…'
            : isMyTurn
              ? 'نوبت شماست'
              : currentTurnSeat !== null && currentTurnSeat !== undefined
                ? 'نوبت ' + (players.find((player) => player?.seat === currentTurnSeat)?.name || 'حریف')
                : 'منتظر حرکت'}
        </div>
        {turnDeadline > 0 && ['playing', 'trump_selection', 'two_player_draft'].includes(publicState?.stage) && (
          <div className={'turn-deadline-chip ' + (turnDeadline - now <= 5 ? 'urgent' : '')}>
            {Math.max(0, Math.ceil(turnDeadline - now))} ثانیه
          </div>
        )}
        {mySeat < 0 && <div className="spectator-chip">تماشاگر</div>}
      </div>

      <div className="royal-table-shell">
        <div className="hokm-table royal-hokm-table">
          <div className="table-watermark" aria-hidden="true">♛</div>
          <div className="table-ornament top-ornament" aria-hidden="true">✦</div>
          <div className="table-ornament bottom-ornament" aria-hidden="true">✦</div>
          {trumpSuit && (
            <div className="trump-badge royal-trump-badge">
              <span>خال حکم</span>
              <strong className={trumpSuit === 'H' || trumpSuit === 'D' ? 'suit-red' : ''}>{SUIT_NAMES[trumpSuit] || trumpSuit}</strong>
            </div>
          )}
          <div className="score-banner royal-score-banner">{renderScore()}</div>

          {isFourPlayer && (myTeam === 0 || myTeam === 1) && (
            <>
              {renderCapturePile(myTeam, ownTricks, myLabel, 'bottom')}
              {renderCapturePile(myTeam === 0 ? 1 : 0, rivalTricks, rivalLabel, 'top')}
            </>
          )}

          <div className="table-seat-top">
            {totalSeats === 4 && renderPlayerPod(getRelativePlayer(2))}
            {totalSeats === 2 && renderPlayerPod(getRelativePlayer(1))}
            {totalSeats === 3 && <>{renderPlayerPod(getRelativePlayer(1))}{renderPlayerPod(getRelativePlayer(2))}</>}
          </div>

          <div className={'table-middle-row ' + (totalSeats === 3 ? 'three-player' : '')}>
            {totalSeats === 4 ? renderPlayerPod(getRelativePlayer(3)) : <div className="side-seat-placeholder" />}
            <div className={'trick-center ' + (resolvedTrick.length ? 'resolving ' : '') +
              (dragSession?.moved ? 'drop-target-ready' : '')}>
              {resolvedTrick.length > 0 && <div className="trick-vfx" aria-hidden="true" />}
              {(currentTrick.length > 0 ? currentTrick : resolvedTrick).map((played: any, idx: number) => {
                const vector = resolvingWinner === null ? [0, 0] : getCollectVector(resolvingWinner);
                return (
                  <div
                    key={String(publicState?.hand_number || 0) + '-' + played.seat + '-' + played.card}
                    className="trick-card-slot"
                    style={{
                      transform: getTrickCardTransform(played.seat),
                      zIndex: idx + 5,
                      '--collect-x': vector[0] + 'px',
                      '--collect-y': vector[1] + 'px',
                      '--deal-delay': (idx * 90) + 'ms',
                    } as React.CSSProperties}
                  >
                    <div className="trick-card-motion">
                      <PlayingCard code={played.card} isPlayable={false} />
                    </div>
                  </div>
                );
              })}
            </div>
            {totalSeats === 4 ? renderPlayerPod(getRelativePlayer(1)) : <div className="side-seat-placeholder" />}
          </div>

          <div className="table-seat-bottom">
            {renderPlayerPod(myPlayer)}
          </div>
          {resolvedTrick.length > 0 && resolvingWinner !== null && (
            <div className="trick-resolution-chip royal-winner-callout">
              ♛ دست برای {players.find((player) => player?.seat === resolvingWinner)?.name || 'بازیکن'} شد
            </div>
          )}
          {isMotionLocked && <div className="collecting-banner">کارت‌ها در حال جمع‌شدن هستند</div>}
        </div>
      </div>

      <section className="hand-section royal-hand-section">
        <div className="hand-heading">
          <b>دست شما</b>
          <span>{myCards.length} کارت</span>
          {isMyTurn && <span className="hand-your-turn">نوبت شما · کارت را بکشید وسط میز</span>}
          {isMotionLocked && <span className="hand-your-turn collecting-tag">در حال نمایش حرکت</span>}
        </div>
        <div className={'my-hand-container royal-hand-container ' + (!isMyTurn ? 'not-your-turn' : '')}>
          <div className="my-hand-fan royal-hand-fan">
            {myCards.map((code) => {
              const isPlayable = isMyTurn && legalMoves.includes(code);
              return (
                <PlayingCard
                  key={code}
                  code={code}
                  isPlayable={isPlayable}
                  isDisabled={!isPlayable}
                  isSelected={selectedCard === code}
                  isDragging={dragSession?.code === code && dragSession.moved}
                  onPointerDown={(event) => startCardDrag(event, code)}
                />
              );
            })}
          </div>
        </div>
        {dragSession?.moved && (
          <div className="drag-ghost" style={{ left: dragSession.x, top: dragSession.y }}>
            <PlayingCard code={dragSession.code} isPlayable={false} />
          </div>
        )}
        {isMyTurn && selectedCard && (
          <div className="selected-card-action">
            <span>انتخاب‌شده: {formatCard(selectedCard)}</span>
            <button className="gold-btn play-selected-btn" onClick={submitSelectedCard}>بازی کردن کارت</button>
            <button className="clear-selection-btn" onClick={() => setSelectedCard(null)}>لغو انتخاب</button>
          </div>
        )}
        {!isMyTurn && publicState?.stage === 'playing' && !isMotionLocked && (
          <div className="waiting-turn-note">کارت‌ها تا رسیدن نوبت شما غیرفعال هستند.</div>
        )}
        {dragHint && <div className="drag-hint">{dragHint}</div>}
      </section>

      <section className={'royal-chat-panel ' + (chatOpen ? 'open' : 'closed')}>
        <button className="chat-panel-heading" onClick={() => setChatOpen((open) => !open)} aria-expanded={chatOpen}>
          <span className="chat-heading-icon">✉</span>
          <span>گفت‌وگوی میز</span>
          <span className="chat-heading-count">{chatMessages.length}</span>
          <span className="chat-expand-indicator">{chatOpen ? '−' : '+'}</span>
        </button>
        {chatOpen && (
          <>
            <div className="chat-message-list" ref={chatScrollRef} aria-live="polite">
              {chatMessages.length === 0 && <div className="chat-empty-state">هنوز پیامی نیست؛ اولین پیام را بفرست.</div>}
              {chatMessages.map((message: any, index: number) => {
                const mine = Number(message.user_id) === Number(myUserId);
                return (
                  <div
                    key={message.id || String(message.user_id) + '-' + String(message.sent_at) + '-' + index}
                    className={'chat-message ' + (mine ? 'mine ' : '') + (message.isReaction ? 'reaction-message' : '')}
                  >
                    {!mine && <span className="chat-message-author">{message.name || 'بازیکن'}</span>}
                    <span className="chat-message-bubble">{message.isReaction ? '✨ ' : ''}{message.text || message.reaction}</span>
                    <time>{formatClock(Number(message.sent_at || 0))}</time>
                  </div>
                );
              })}
            </div>
            <div className="chat-quick-actions">
              {QUICK_REACTIONS.map((reaction) => (
                <button key={reaction} type="button" onClick={() => onSendChat(reaction)}>{reaction}</button>
              ))}
            </div>
            <form className="chat-compose" onSubmit={sendChat}>
              <input
                value={chatDraft}
                onChange={(event) => setChatDraft(event.target.value)}
                maxLength={150}
                placeholder="پیام خود را بنویس..."
                aria-label="پیام چت"
              />
              <button type="submit" disabled={!chatDraft.trim()} aria-label="ارسال پیام">➤</button>
            </form>
          </>
        )}
      </section>
    </div>
  );
};
