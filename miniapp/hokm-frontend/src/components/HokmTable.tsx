import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { PlayingCard } from './Cards';
import { sounds, triggerHaptic } from './AudioHaptics';

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
  grabOffsetX: number;
  grabOffsetY: number;
  width: number;
  height: number;
  moved: boolean;
}

const SUITS: Record<string, { name: string; symbol: string; red: boolean }> = {
  S: { name: 'پیک', symbol: '♠', red: false },
  H: { name: 'دل', symbol: '♥', red: true },
  D: { name: 'خشت', symbol: '♦', red: true },
  C: { name: 'گشنیز', symbol: '♣', red: false },
};
const QUICK_REACTIONS = ['👏', '🔥', '👑', 'ایول!', 'دمت گرم', 'خسته نباشی'];
const CAPTURE_CARD_STYLES = Array.from({ length: 5 }, (_, index): React.CSSProperties => ({
  transform:
    'translate(' + (index * 3.2) + 'px, ' + (-index * 2.1) +
    'px) rotate(' + ((index - 2) * 3) + 'deg)',
}));

const suitLabel = (suit?: string) => {
  if (!suit) return '';
  const info = SUITS[suit];
  return info ? info.name + ' ' + info.symbol : suit;
};

const formatCard = (code: string) => code.slice(0, -1) + ' ' + suitLabel(code.slice(-1));

const formatTime = (timestamp: number) => {
  if (!timestamp) return '';
  return new Date(timestamp * 1000).toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' });
};

const TurnCountdown = memo(({ deadline }: { deadline: number }) => {
  const [seconds, setSeconds] = useState(() =>
    deadline > 0 ? Math.max(0, Math.ceil(deadline - Date.now() / 1000)) : 0,
  );

  useEffect(() => {
    if (!deadline) {
      setSeconds(0);
      return;
    }
    const update = () => setSeconds(Math.max(0, Math.ceil(deadline - Date.now() / 1000)));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  return (
    <span className={'turn-countdown ' + (seconds > 0 && seconds <= 5 ? 'urgent' : '')}>
      <span className="turn-countdown-dot" />
      {seconds > 0 ? seconds + ' ثانیه' : 'بدون محدودیت'}
    </span>
  );
});
TurnCountdown.displayName = 'TurnCountdown';

const setGhostPosition = (element: HTMLDivElement, session: DragSession) => {
  element.style.transform =
    'translate3d(' +
    (session.x - session.grabOffsetX) + 'px,' +
    (session.y - session.grabOffsetY) + 'px,0) rotate(-2deg)';
};

const initializeGhost = (element: HTMLDivElement, session: DragSession) => {
  element.style.width = session.width + 'px';
  element.style.height = session.height + 'px';
  element.style.transformOrigin = session.grabOffsetX + 'px ' + session.grabOffsetY + 'px';
  setGhostPosition(element, session);
};

const HokmTableComponent: React.FC<HokmTableProps> = ({
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
  const [motionLocked, setMotionLocked] = useState(false);
  const [chatDraft, setChatDraft] = useState('');
  const [chatOpen, setChatOpen] = useState(true);
  const [dragPreviewVisible, setDragPreviewVisible] = useState(false);
  const [dragHint, setDragHint] = useState('');
  const [dropTargetActive, setDropTargetActive] = useState(false);

  const previousTrickLengthRef = useRef(0);
  const lastCompletedKeyRef = useRef<string | null>(null);
  const dragRef = useRef<DragSession | null>(null);
  const dragGhostRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const chatInputRef = useRef<HTMLInputElement | null>(null);
  const motionTimerRef = useRef<number | null>(null);
  const dropZoneRef = useRef<HTMLDivElement | null>(null);

  const players: any[] = Array.isArray(publicState?.players) ? publicState.players.filter(Boolean) : [];
  const mySeat = Number.isInteger(privateState?.seat) ? privateState.seat : -1;
  const layoutSeat = mySeat >= 0 ? mySeat : 0;
  const myPlayer = players.find((player) => player?.seat === mySeat);
  const myTeam = myPlayer?.team;
  const viewerTeam = myTeam === 1 ? 1 : 0;
  const gameMode = String(publicState?.game_mode || 'hokm_4p');
  const isFourPlayer = gameMode === 'hokm_4p';
  const totalSeats = isFourPlayer ? 4 : gameMode === 'hokm_2p' ? 2 : 3;
  const cards: string[] = Array.isArray(privateState?.cards) ? privateState.cards : [];
  const legalMoves: string[] = Array.isArray(privateState?.legal_moves) ? privateState.legal_moves : [];
  const currentTrick: any[] = Array.isArray(publicState?.current_trick) ? publicState.current_trick : [];
  const lastCompleted = publicState?.last_completed_trick || null;
  const trumpSuit = publicState?.trump_suit as string | undefined;
  const currentTurnSeat = publicState?.current_turn_seat;
  const turnDeadline = Number(publicState?.action_deadline || 0);
  const resolutionUntil = Number(publicState?.trick_resolution_until || 0);
  const seq = Number(publicState?.seq || 0);
  const isMyTurn = currentTurnSeat === mySeat && !motionLocked;
  const interactionRef = useRef({ isMyTurn, legalMoves, onPlayCard });
  interactionRef.current = { isMyTurn, legalMoves, onPlayCard };

  const phase = useMemo(() => {
    const labels: Record<string, string> = {
      waiting: 'اتاق انتظار',
      ceremony: 'تعیین حاکم',
      trump_selection: 'انتخاب حکم',
      two_player_draft: 'انتخاب کارت',
      dealing: 'پخش کارت',
      playing: 'در جریان بازی',
      hand_ended: 'پایان دست',
      match_ended: 'پایان مسابقه',
    };
    return labels[publicState?.stage] || 'آماده‌سازی میز';
  }, [publicState?.stage]);

  const teamATricks = players.reduce(
    (sum, player) => sum + (player?.team === 0 ? Number(player.tricks_won || 0) : 0),
    0,
  );
  const teamBTricks = players.reduce(
    (sum, player) => sum + (player?.team === 1 ? Number(player.tricks_won || 0) : 0),
    0,
  );
  const resolvingWinnerTeam = resolvingWinner === null
    ? null
    : players.find((player) => player?.seat === resolvingWinner)?.team;
  const displayedTeamATricks = Math.max(0, teamATricks - (resolvedTrick.length && resolvingWinnerTeam === 0 ? 1 : 0));
  const displayedTeamBTricks = Math.max(0, teamBTricks - (resolvedTrick.length && resolvingWinnerTeam === 1 ? 1 : 0));
  const teamScoreA = Number(publicState?.team_a_match_points || 0);
  const teamScoreB = Number(publicState?.team_b_match_points || 0);
  const ownTricks = viewerTeam === 0 ? displayedTeamATricks : displayedTeamBTricks;
  const rivalTricks = viewerTeam === 0 ? displayedTeamBTricks : displayedTeamATricks;

  const relativePlayer = useCallback((offset: number) => {
    const seat = (layoutSeat + offset + totalSeats) % totalSeats;
    return players.find((player) => player?.seat === seat);
  }, [layoutSeat, players, totalSeats]);

  useEffect(() => {
    setSelectedCard(null);
    setDragHint('');
  }, [seq, currentTurnSeat, cards.length]);

  useEffect(() => {
    if (motionTimerRef.current !== null) {
      window.clearTimeout(motionTimerRef.current);
      motionTimerRef.current = null;
    }
    const remaining = resolutionUntil * 1000 - Date.now();
    if (remaining <= 0) {
      setMotionLocked(false);
      return;
    }
    setMotionLocked(true);
    motionTimerRef.current = window.setTimeout(() => {
      setMotionLocked(false);
      motionTimerRef.current = null;
    }, remaining);
    return () => {
      if (motionTimerRef.current !== null) {
        window.clearTimeout(motionTimerRef.current);
        motionTimerRef.current = null;
      }
    };
  }, [resolutionUntil]);

  useEffect(() => {
    const completedNow = previousTrickLengthRef.current > 0 && currentTrick.length === 0;
    previousTrickLengthRef.current = currentTrick.length;
    if (!completedNow || !lastCompleted?.cards?.length) return;

    const key = String(publicState?.hand_number || 0) + ':' +
      lastCompleted.cards.map((card: any) => String(card.seat) + '-' + card.card).join('|');
    if (lastCompletedKeyRef.current === key) return;

    lastCompletedKeyRef.current = key;
    setResolvedTrick(lastCompleted.cards);
    setResolvingWinner(lastCompleted.winner_seat ?? null);
    sounds.playTrickWin();
    triggerHaptic('success');

    const timer = window.setTimeout(() => {
      setResolvedTrick([]);
      setResolvingWinner(null);
    }, 2320);
    return () => window.clearTimeout(timer);
  }, [seq, currentTrick.length, lastCompleted, publicState?.hand_number]);

  useEffect(() => {
    if (!chatOpen || !chatScrollRef.current) return;
    const element = chatScrollRef.current;
    element.scrollTo({ top: element.scrollHeight, behavior: 'smooth' });
  }, [chatMessages.length, chatOpen]);

  const updateDragGhost = useCallback(() => {
    rafRef.current = null;
    const session = dragRef.current;
    const ghost = dragGhostRef.current;
    if (session?.moved && ghost) setGhostPosition(ghost, session);
  }, []);

  const scheduleDragGhostUpdate = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = window.requestAnimationFrame(updateDragGhost);
  }, [updateDragGhost]);

  const clearDrag = useCallback(() => {
    dragRef.current = null;
    if (rafRef.current !== null) {
      window.cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setDragPreviewVisible(false);
    setDropTargetActive(false);
  }, []);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      const active = dragRef.current;
      if (!active || active.pointerId !== event.pointerId) return;

      const distance = Math.hypot(event.clientX - active.startX, event.clientY - active.startY);
      const moved = active.moved || distance >= 7;
      dragRef.current = { ...active, x: event.clientX, y: event.clientY, moved };

      if (moved) {
        event.preventDefault();
        if (!active.moved) setDragPreviewVisible(true);
        const zone = dropZoneRef.current?.getBoundingClientRect();
        const inside = Boolean(
          zone &&
          event.clientX >= zone.left - 22 &&
          event.clientX <= zone.right + 22 &&
          event.clientY >= zone.top - 22 &&
          event.clientY <= zone.bottom + 22,
        );
        setDropTargetActive((current) => current !== inside ? inside : current);
        scheduleDragGhostUpdate();
      }
    };

    const onPointerUp = (event: PointerEvent) => {
      const active = dragRef.current;
      if (!active || active.pointerId !== event.pointerId) return;

      const distance = Math.hypot(event.clientX - active.startX, event.clientY - active.startY);
      const moved = active.moved || distance >= 7;
      const finalSession = { ...active, x: event.clientX, y: event.clientY, moved };
      dragRef.current = finalSession;

      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }

      const rect = dropZoneRef.current?.getBoundingClientRect();
      const padding = Math.max(16, (rect?.width || 100) * 0.12);
      const droppedOnCenter = Boolean(
        moved && rect &&
        event.clientX >= rect.left - padding &&
        event.clientX <= rect.right + padding &&
        event.clientY >= rect.top - padding &&
        event.clientY <= rect.bottom + padding,
      );

      dragRef.current = null;
      setDragPreviewVisible(false);
      setDropTargetActive(false);

      const currentInteraction = interactionRef.current;
      if (!moved) {
        setSelectedCard(finalSession.code);
        setDragHint('');
        triggerHaptic('light');
        return;
      }

      if (
        droppedOnCenter &&
        currentInteraction.isMyTurn &&
        currentInteraction.legalMoves.includes(finalSession.code)
      ) {
        setSelectedCard(null);
        setDragHint('');
        sounds.playCardSlap();
        triggerHaptic('medium');
        currentInteraction.onPlayCard(finalSession.code);
      } else {
        setSelectedCard(finalSession.code);
        setDragHint(droppedOnCenter ? 'این کارت در حال حاضر مجاز نیست.' : 'کارت را روی محدوده‌ی مرکزی میز رها کن.');
        triggerHaptic('light');
      }
    };

    const onPointerCancel = (event: PointerEvent) => {
      if (dragRef.current?.pointerId === event.pointerId) clearDrag();
    };
    const onWindowBlur = () => {
      if (dragRef.current) clearDrag();
    };

    window.addEventListener('pointermove', onPointerMove, { passive: false });
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerCancel);
    window.addEventListener('blur', onWindowBlur);
    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('pointercancel', onPointerCancel);
      window.removeEventListener('blur', onWindowBlur);
    };
  }, [clearDrag, scheduleDragGhostUpdate]);

  useEffect(() => () => {
    if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current);
    if (motionTimerRef.current !== null) window.clearTimeout(motionTimerRef.current);
  }, []);

  const beginDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest<HTMLButtonElement>('.playing-card[data-card-code]');
    if (!button || !event.currentTarget.contains(button)) return;

    const code = button.dataset.cardCode || '';
    const currentInteraction = interactionRef.current;
    if (!code || !currentInteraction.isMyTurn ||
        !currentInteraction.legalMoves.includes(code) || event.button !== 0) return;

    event.preventDefault();
    const rect = button.getBoundingClientRect();
    dragRef.current = {
      code,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      x: event.clientX,
      y: event.clientY,
      grabOffsetX: event.clientX - rect.left,
      grabOffsetY: event.clientY - rect.top,
      width: rect.width,
      height: rect.height,
      moved: false,
    };
    setSelectedCard(null);
    setDragHint('');
    try {
      button.setPointerCapture(event.pointerId);
    } catch {}
  };

  const handleCardKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest<HTMLButtonElement>('.playing-card[data-card-code]');
    if (!button || !event.currentTarget.contains(button)) return;

    const code = button.dataset.cardCode || '';
    const currentInteraction = interactionRef.current;
    if (!code || !currentInteraction.isMyTurn ||
        !currentInteraction.legalMoves.includes(code)) return;

    event.preventDefault();
    setSelectedCard((current) => current === code ? null : code);
    triggerHaptic('light');
  };

  const playSelectedCard = () => {
    if (!selectedCard || !isMyTurn || !legalMoves.includes(selectedCard)) return;
    const code = selectedCard;
    setSelectedCard(null);
    setDragHint('');
    sounds.playCardSlap();
    triggerHaptic('medium');
    onPlayCard(code);
  };

  const relativeOffset = (seat: number) => (seat - layoutSeat + totalSeats) % totalSeats;
  const trickCardTransform = (seat: number) => {
    const offset = relativeOffset(seat);
    if (totalSeats === 4) {
      if (offset === 0) return 'translateY(35px) rotate(-4deg)';
      if (offset === 1) return 'translateX(43px) rotate(4deg)';
      if (offset === 2) return 'translateY(-35px) rotate(4deg)';
      return 'translateX(-43px) rotate(-4deg)';
    }
    if (totalSeats === 3) {
      if (offset === 0) return 'translateY(32px)';
      if (offset === 1) return 'translate(40px, -25px)';
      return 'translate(-40px, -25px)';
    }
    return offset === 0 ? 'translateY(28px)' : 'translateY(-28px)';
  };

  const collectVector = (winnerSeat: number): [number, number] => {
    if (totalSeats === 4) {
      const team = players.find((player) => player?.seat === winnerSeat)?.team;
      return team === viewerTeam ? [-170, 130] : [170, -130];
    }
    const offset = relativeOffset(winnerSeat);
    return offset === 1 ? [100, -100] : offset === 2 ? [-100, -100] : [0, 125];
  };

  const renderPlayer = (player: any, position: string) => {
    if (!player) return null;
    const active = currentTurnSeat === player.seat;
    const hakem = publicState?.hakem_seat === player.seat;
    const winner = resolvedTrick.length > 0 && resolvingWinner === player.seat;
    const ownTeam = isFourPlayer && player.team === myTeam;

    return (
      <div key={player.seat} className={'table-player ' + position +
        (active ? ' is-active' : '') + (winner ? ' is-winner' : '') + (ownTeam ? ' is-friendly' : '')}
      >
        <div className="player-avatar">
          <span>{player.is_ai ? '🤖' : '👤'}</span>
          {hakem && <span className="player-crown">♛</span>}
          <span className={'player-presence ' + (player.is_connected === false ? 'offline' : '')} />
        </div>
        <div className="player-meta">
          <b className="player-name">{player.name}</b>
          <span className="player-detail">
            {isFourPlayer
              ? Number(player.cards_remaining || 0) + ' کارت باقی'
              : Number(player.tricks_won || 0) + ' دور · ' + Number(player.cards_remaining || 0) + ' کارت'}
          </span>
        </div>
        {hakem && <span className="player-hakem-tag">حاکم</span>}
        {winner && <span className="player-win-tag">برنده‌ی دور</span>}
      </div>
    );
  };

  const renderCapturePile = (team: number, count: number, position: 'top' | 'bottom') => (
    <div
      className={'capture-pile ' + position + ' ' + (team === viewerTeam ? 'own' : 'rival') +
        (resolvedTrick.length && resolvingWinnerTeam === team ? ' receiving' : '')}
      data-team={team}
      aria-label={'تیم ' + (team === 0 ? 'الف' : 'ب') + ': ' + count + ' دور جمع‌شده'}
    >
      <div className="capture-stack">
        {Array.from({ length: Math.min(count, 5) }).map((_, index) => (
          <PlayingCard
            key={String(team) + '-' + String(index)}
            code="BACK"
            isPlayable={false}
            style={CAPTURE_CARD_STYLES[index]}
          />
        ))}
        <span className="capture-total">{count}</span>
      </div>
      <span className="capture-caption">{team === viewerTeam ? 'تیم شما' : 'حریف'}</span>
    </div>
  );

  const sendChat = (event: React.FormEvent) => {
    event.preventDefault();
    const message = chatDraft.trim().slice(0, 150);
    if (!message) return;
    onSendChat(message);
    setChatDraft('');
    chatInputRef.current?.focus();
  };

  const statusText = motionLocked
    ? 'در حال جمع‌شدن کارت‌ها'
    : isMyTurn
      ? 'نوبت شماست'
      : currentTurnSeat !== null && currentTurnSeat !== undefined
        ? 'نوبت ' + (players.find((player) => player?.seat === currentTurnSeat)?.name || 'حریف')
        : 'منتظر حرکت';

  return (
    <div className="game-layout">
      <header className="game-statusbar">
        <div className="statusbar-main">
          <span className="status-eyebrow">ROYAL HOKM</span>
          <span className="status-phase">{phase}</span>
        </div>
        <div className="statusbar-indicators">
          {trumpSuit && (
            <span className="status-chip trump-chip">
              <span>حکم</span>
              <b className={SUITS[trumpSuit]?.red ? 'suit-red' : ''}>{suitLabel(trumpSuit)}</b>
            </span>
          )}
          <span className={'status-chip turn-chip ' + (isMyTurn ? 'your-turn' : '') + (motionLocked ? ' is-resolving' : '')}>
            <i className="turn-indicator" />
            {statusText}
          </span>
          {mySeat < 0 && <span className="status-chip spectator-chip">تماشاگر</span>}
          {turnDeadline > 0 && ['playing', 'trump_selection', 'two_player_draft'].includes(publicState?.stage) && (
            <TurnCountdown deadline={turnDeadline} />
          )}
        </div>
      </header>

      <div className="game-content">
        <main className="game-main">
          <section className="table-frame" aria-label="میز بازی حکم">
            <div className="table-rim">
              <div className="table-felt">
                <div className="felt-grain" aria-hidden="true" />
                <div className="felt-center-emblem" aria-hidden="true"><span>♛</span><i /></div>
                <div className="felt-corner top-left" aria-hidden="true">✦</div>
                <div className="felt-corner top-right" aria-hidden="true">✦</div>
                <div className="felt-corner bottom-left" aria-hidden="true">✦</div>
                <div className="felt-corner bottom-right" aria-hidden="true">✦</div>

                <div className="table-top-info">
                  <div className="match-score">
                    {isFourPlayer ? (
                      <>
                        <div className={'score-team ' + (viewerTeam === 0 ? 'mine' : '')}>
                          <span className="score-team-name">{viewerTeam === 0 ? 'تیم شما' : 'تیم الف'}</span>
                          <b>{teamScoreA}</b>
                        </div>
                        <span className="score-divider">:</span>
                        <div className={'score-team ' + (viewerTeam === 1 ? 'mine' : '')}>
                          <span className="score-team-name">{viewerTeam === 1 ? 'تیم شما' : 'تیم ب'}</span>
                          <b>{teamScoreB}</b>
                        </div>
                      </>
                    ) : (
                      <span className="individual-score-title">امتیاز مسابقه</span>
                    )}
                  </div>
                  {trumpSuit && (
                    <div className="table-trump-medallion">
                      <span>خال حکم</span>
                      <b className={SUITS[trumpSuit]?.red ? 'suit-red' : ''}>{SUITS[trumpSuit]?.symbol}</b>
                    </div>
                  )}
                </div>

                {isFourPlayer && (
                  <>
                    {renderCapturePile(viewerTeam, ownTricks, 'bottom')}
                    {renderCapturePile(viewerTeam === 0 ? 1 : 0, rivalTricks, 'top')}
                  </>
                )}

                <div className="table-seats">
                  {totalSeats === 4 && renderPlayer(relativePlayer(2), 'seat-top')}
                  {totalSeats === 2 && renderPlayer(relativePlayer(1), 'seat-top')}
                  {totalSeats === 3 && (
                    <>
                      {renderPlayer(relativePlayer(1), 'seat-three-left')}
                      {renderPlayer(relativePlayer(2), 'seat-three-right')}
                    </>
                  )}
                  {totalSeats === 4 && renderPlayer(relativePlayer(3), 'seat-left')}
                  {totalSeats === 4 && renderPlayer(relativePlayer(1), 'seat-right')}
                  {renderPlayer(myPlayer, 'seat-bottom')}
                </div>

                <div
                  className={'trick-center ' + (resolvedTrick.length ? 'is-resolving ' : '') +
                    (dropTargetActive ? 'drop-active' : '')}
                  ref={dropZoneRef}
                  aria-label="محدوده‌ی انداختن کارت"
                >
                  <div className="trick-drop-ring" aria-hidden="true" />
                  {dropTargetActive && <div className="drop-halo" aria-hidden="true" />}
                  {resolvedTrick.length > 0 && <div className="trick-burst" aria-hidden="true" />}
                  {(currentTrick.length > 0 ? currentTrick : resolvedTrick).map((played: any, index: number) => {
                    const vector = resolvingWinner === null ? [0, 0] : collectVector(resolvingWinner);
                    return (
                      <div
                        key={String(publicState?.hand_number || 0) + '-' + played.seat + '-' + played.card}
                        className="trick-card-position"
                        style={{
                          transform: trickCardTransform(played.seat),
                          zIndex: index + 4,
                          '--collect-x': vector[0] + 'px',
                          '--collect-y': vector[1] + 'px',
                          '--card-delay': (index * 85) + 'ms',
                        } as React.CSSProperties}
                      >
                        <div className="trick-card-arrive">
                          <PlayingCard code={played.card} isPlayable={false} />
                        </div>
                      </div>
                    );
                  })}
                  {currentTrick.length === 0 && resolvedTrick.length === 0 && (
                    <div className="trick-empty-mark">
                      <span>✦</span>
                      <small>{dropTargetActive ? 'اینجا رها کن' : 'میدان بازی'}</small>
                    </div>
                  )}
                </div>

                {resolvedTrick.length > 0 && resolvingWinner !== null && (
                  <div className="trick-winner-banner">
                    <span>♛</span>
                    <b>{players.find((player) => player?.seat === resolvingWinner)?.name || 'بازیکن'}</b>
                    <small>این دور را برد</small>
                  </div>
                )}
                {motionLocked && <div className="motion-lock-label">کارت‌ها در حال جمع‌شدن هستند…</div>}
              </div>
            </div>
          </section>

          <section className="player-hand-panel">
            <div className="hand-panel-heading">
              <div><span className="hand-overline">YOUR HAND</span><h2>کارت‌های شما</h2></div>
              <div className="hand-heading-meta">
                <span className="hand-count">{cards.length} <small>کارت</small></span>
                {isMyTurn && !motionLocked && <span className="hand-turn-badge">نوبت شما</span>}
                {motionLocked && <span className="hand-turn-badge resolving">در حال اجرا</span>}
              </div>
            </div>

            <div className={'hand-scroll-area ' + (!isMyTurn ? 'not-turn' : '')}>
              <div className="hand-cards" onPointerDown={beginDrag} onKeyDown={handleCardKeyDown}>
                {cards.map((code) => {
                  const playable = isMyTurn && legalMoves.includes(code);
                  return (
                    <PlayingCard
                      key={code}
                      code={code}
                      isPlayable={playable}
                      isDisabled={!playable}
                      isSelected={selectedCard === code}
                      isDragging={dragRef.current?.code === code && dragPreviewVisible}
                    />
                  );
                })}
              </div>
            </div>

            <div className="hand-action-row">
              <div className="hand-instruction">
                {isMyTurn ? 'کارت را بکش و وسط میز رها کن، یا انتخابش کن.' : 'با رسیدن نوبت شما کارت‌ها فعال می‌شوند.'}
              </div>
              {selectedCard && isMyTurn && (
                <div className="selected-card-actions">
                  <span className="selected-card-label">{formatCard(selectedCard)}</span>
                  <button className="action-primary" onClick={playSelectedCard}>بازی کارت</button>
                  <button className="action-quiet" onClick={() => setSelectedCard(null)}>لغو</button>
                </div>
              )}
            </div>
            {dragHint && <div className="inline-hint" role="status">{dragHint}</div>}
          </section>
        </main>

        <aside className={'chat-panel ' + (chatOpen ? 'expanded' : 'collapsed')}>
          <button className="chat-panel-title" onClick={() => setChatOpen((open) => !open)} aria-expanded={chatOpen}>
            <span className="chat-icon">✉</span>
            <span className="chat-title-text"><b>گفت‌وگوی میز</b><small>ارتباط زنده‌ی بازیکنان</small></span>
            <span className="chat-message-count">{chatMessages.length}</span>
            <span className="chat-chevron">{chatOpen ? '−' : '+'}</span>
          </button>
          {chatOpen && (
            <>
              <div className="chat-messages" ref={chatScrollRef} aria-live="polite">
                {chatMessages.length === 0 && (
                  <div className="chat-empty">
                    <span>✧</span><b>میز هنوز ساکت است</b><small>اولین پیام را بفرست و بازی را گرم کن.</small>
                  </div>
                )}
                {chatMessages.map((message: any, index: number) => {
                  const mine = Number(message.user_id) === Number(myUserId);
                  const content = message.text || message.reaction || '';
                  return (
                    <div
                      key={message.id || String(message.user_id) + '-' + String(message.sent_at) + '-' + index}
                      className={'chat-message ' + (mine ? 'mine ' : '') + (message.isReaction ? 'reaction' : '')}
                    >
                      {!mine && <span className="chat-author">{message.name || 'بازیکن'}</span>}
                      <span className="chat-bubble">{message.isReaction ? '✦ ' : ''}{content}</span>
                      <time>{formatTime(Number(message.sent_at || 0))}</time>
                    </div>
                  );
                })}
              </div>
              <div className="chat-reactions">
                {QUICK_REACTIONS.map((reaction) => (
                  <button key={reaction} type="button" onClick={() => onSendChat(reaction)}>{reaction}</button>
                ))}
              </div>
              <form className="chat-input-row" onSubmit={sendChat}>
                <input
                  ref={chatInputRef}
                  value={chatDraft}
                  onChange={(event) => setChatDraft(event.target.value)}
                  maxLength={150}
                  placeholder="پیامت را بنویس..."
                  aria-label="پیام چت"
                />
                <button type="submit" disabled={!chatDraft.trim()} aria-label="ارسال پیام">➤</button>
              </form>
            </>
          )}
        </aside>
      </div>

      {dragPreviewVisible && dragRef.current && createPortal(
        <div
          className="drag-preview-layer"
          ref={(element) => {
            dragGhostRef.current = element;
            if (element && dragRef.current) initializeGhost(element, dragRef.current);
          }}
          aria-hidden="true"
        >
          <PlayingCard code={dragRef.current.code} isPlayable={false} className="drag-preview-card" />
        </div>,
        document.body,
      )}
    </div>
  );
};

export const HokmTable = React.memo(HokmTableComponent);
