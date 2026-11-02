import React, { useEffect, useRef, useState } from 'react';

interface HokmLobbyProps {
  user: any;
  ratings: any;
  activeRoom: any;
  onCreateRoom: (mode: string, timeLimit: number, isRanked: boolean) => void;
  onStartSoloAi: (mode: string, difficulty: string, timeLimit: number) => void;
  onJoinRoom: (code: string) => void;
  onAddAi: (difficulty: string) => void;
  onStartGame: () => void;
  onLeaveRoom: () => void;
}

const MODES = [
  { id: 'hokm_4p', title: 'چهارنفره تیمی', subtitle: 'قانون کلاسیک', icon: '♛', players: '۴ بازیکن' },
  { id: 'hokm_3p', title: 'سه‌نفره', subtitle: '۵۱ کارت', icon: '♠', players: '۳ بازیکن' },
  { id: 'hokm_2p', title: 'دونفره', subtitle: 'انتخاب کارت', icon: '♦', players: '۲ بازیکن' },
] as const;

const DIFFICULTIES = [
  { key: 'easy', label: 'آسان', description: 'برای شروع' },
  { key: 'medium', label: 'متوسط', description: 'متعادل' },
  { key: 'hard', label: 'حرفه‌ای', description: 'چالش واقعی' },
];

const TIME_LIMITS = [
  { value: 15, label: '۱۵ ثانیه' },
  { value: 30, label: '۳۰ ثانیه' },
  { value: 45, label: '۴۵ ثانیه' },
  { value: 60, label: '۶۰ ثانیه' },
  { value: 0, label: 'آزاد' },
];

export const HokmLobby: React.FC<HokmLobbyProps> = ({
  user,
  ratings,
  activeRoom,
  onCreateRoom,
  onStartSoloAi,
  onJoinRoom,
  onAddAi,
  onStartGame,
  onLeaveRoom,
}) => {
  const [selectedMode, setSelectedMode] = useState<'hokm_4p' | 'hokm_3p' | 'hokm_2p'>('hokm_4p');
  const [turnTime, setTurnTime] = useState(30);
  const [isRanked, setIsRanked] = useState(true);
  const [inviteCodeInput, setInviteCodeInput] = useState('');
  const [aiDifficulty, setAiDifficulty] = useState('medium');
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState('');
  const copiedTimerRef = useRef<number | null>(null);

  const modeRating = ratings?.[selectedMode] || { rating: 1000, tier: 'Bronze' };

  useEffect(() => () => {
    if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
  }, []);

  const copyRoomCode = async () => {
    if (!activeRoom?.invite_code) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable');
      await navigator.clipboard.writeText(String(activeRoom.invite_code));
      setCopied(true);
      setCopyError('');
      if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
      copiedTimerRef.current = window.setTimeout(() => {
        setCopied(false);
        copiedTimerRef.current = null;
      }, 1800);
    } catch {
      setCopyError('کپی خودکار در این مرورگر در دسترس نیست.');
    }
  };

  const submitJoin = (event: React.FormEvent) => {
    event.preventDefault();
    const code = inviteCodeInput.trim().toUpperCase();
    if (code.length === 6) onJoinRoom(code);
  };

  const modeTitle = MODES.find((mode) => mode.id === selectedMode)?.title || 'حکم';

  return (
    <div className="lobby-layout mx-auto flex w-full max-w-5xl flex-col gap-5">
      <section className="lobby-profile-card flex items-center">
        <div className="lobby-profile-orb" aria-hidden="true">♛</div>
        <div className="lobby-profile-copy">
          <span className="lobby-overline">WELCOME TO THE TABLE</span>
          <h1>سلام، {user?.name || 'بازیکن'}!</h1>
          <p>جای خودت را انتخاب کن؛ بازی نزدیک است.</p>
        </div>
        <div className="lobby-rating-card">
          <span>رتبه‌ی شما</span>
          <b>{modeRating.tier}</b>
          <strong>{modeRating.rating}</strong>
        </div>
      </section>

      {activeRoom && activeRoom.stage === 'waiting' ? (
        <section className="waiting-room-card">
          <div className="section-heading">
            <div>
              <span className="lobby-overline">PRIVATE TABLE</span>
              <h2>اتاق انتظار</h2>
            </div>
            <span className="waiting-live-pill"><i /> در انتظار بازیکن</span>
          </div>

          <div className="invite-code-panel">
            <div>
              <span>کد دعوت دوستان</span>
              <b dir="ltr">{activeRoom.invite_code}</b>
            </div>
            <button type="button" className="button-secondary copy-code-button" onClick={copyRoomCode}>
              {copied ? '✓ کپی شد' : 'کپی کد'}
            </button>
          </div>
          {copyError && <p className="form-hint error-hint">{copyError}</p>}

          <div className="roster-heading">
            <b>جایگاه بازیکنان</b>
            <span>{(activeRoom.players || []).filter(Boolean).length} از {activeRoom.players?.length || 0}</span>
          </div>
          <div className="waiting-roster grid gap-2">
            {activeRoom.players?.map((player: any, index: number) => (
              <div className={'roster-seat ' + (player ? 'occupied' : 'empty')} key={index}>
                <span className="roster-avatar">{player ? (player.is_ai ? '🤖' : '👤') : '＋'}</span>
                <span className="roster-name">
                  <b>{player?.name || 'جایگاه آزاد'}</b>
                  <small>
                    {player
                      ? (player.team !== null && player.team !== undefined
                          ? 'تیم ' + (player.team === 0 ? 'الف' : 'ب')
                          : 'بازیکن')
                      : 'منتظر بازیکن یا ربات'}
                  </small>
                </span>
                <span className="roster-seat-index">{String(index + 1).padStart(2, '0')}</span>
              </div>
            ))}
          </div>

          <div className="waiting-ai-controls">
            <label htmlFor="waiting-ai-level">افزودن حریف یا هم‌تیمی هوشمند</label>
            <div className="waiting-ai-row">
              <select id="waiting-ai-level" value={aiDifficulty} onChange={(event) => setAiDifficulty(event.target.value)}>
                {DIFFICULTIES.map((difficulty) => (
                  <option value={difficulty.key} key={difficulty.key}>ربات {difficulty.label}</option>
                ))}
              </select>
              <button type="button" className="button-secondary" onClick={() => onAddAi(aiDifficulty)}>+ افزودن ربات</button>
            </div>
          </div>

          <div className="waiting-actions">
            <button type="button" className="button-primary" onClick={onStartGame}>شروع مسابقه <span>←</span></button>
            <button type="button" className="button-ghost danger-ghost" onClick={onLeaveRoom}>خروج و لغو اتاق</button>
          </div>
        </section>
      ) : (
        <>
          <section className="lobby-section">
            <div className="section-heading">
              <div>
                <span className="lobby-overline">CHOOSE YOUR FORMAT</span>
                <h2>نوع بازی را انتخاب کن</h2>
              </div>
              <span className="section-step">۰۱ / ۰۳</span>
            </div>
            <div className="mode-grid grid grid-cols-3 gap-3">
              {MODES.map((mode) => (
                <button
                  type="button"
                  key={mode.id}
                  className={'mode-card ' + (selectedMode === mode.id ? 'selected' : '')}
                  onClick={() => setSelectedMode(mode.id)}
                  aria-pressed={selectedMode === mode.id}
                >
                  <span className="mode-card-icon">{mode.icon}</span>
                  <b>{mode.title}</b>
                  <small>{mode.subtitle}</small>
                  <span className="mode-card-players">{mode.players}</span>
                  <span className="mode-selected-check">✓</span>
                </button>
              ))}
            </div>
          </section>

          <section className="lobby-section settings-section">
            <div className="section-heading">
              <div>
                <span className="lobby-overline">MATCH SETTINGS</span>
                <h2>تنظیمات مسابقه</h2>
              </div>
              <span className="section-step">۰۲ / ۰۳</span>
            </div>

            <div className="settings-grid grid grid-cols-2 gap-3">
              <div className="setting-card">
                <div className="setting-title">
                  <span>درجه‌ی هوش مصنوعی</span>
                  <small>برای بازی سریع</small>
                </div>
                <div className="choice-grid difficulty-grid">
                  {DIFFICULTIES.map((difficulty) => (
                    <button
                      key={difficulty.key}
                      type="button"
                      className={'choice-button ' + (aiDifficulty === difficulty.key ? 'active' : '')}
                      onClick={() => setAiDifficulty(difficulty.key)}
                      aria-pressed={aiDifficulty === difficulty.key}
                    >
                      <b>{difficulty.label}</b>
                      <small>{difficulty.description}</small>
                    </button>
                  ))}
                </div>
              </div>

              <div className="setting-card">
                <div className="setting-title">
                  <span>محدودیت زمان حرکت</span>
                  <small>هر نوبت</small>
                </div>
                <div className="choice-grid time-grid">
                  {TIME_LIMITS.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      className={'choice-button time-choice ' + (turnTime === option.value ? 'active' : '')}
                      onClick={() => setTurnTime(option.value)}
                      aria-pressed={turnTime === option.value}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <button
              type="button"
              className={'ranked-toggle ' + (isRanked ? 'enabled' : '')}
              onClick={() => setIsRanked((value) => !value)}
              aria-pressed={isRanked}
            >
              <span className="ranked-toggle-icon">♜</span>
              <span><b>مسابقه‌ی امتیازی</b><small>{isRanked ? 'نتیجه روی رتبه‌ی شما اثر می‌گذارد.' : 'بازی دوستانه؛ بدون تغییر رتبه.'}</small></span>
              <i className="toggle-track"><em /></i>
            </button>
          </section>

          <section className="lobby-section action-section">
            <div className="section-heading">
              <div>
                <span className="lobby-overline">READY TO PLAY</span>
                <h2>میز {modeTitle} آماده است</h2>
              </div>
              <span className="section-step">۰۳ / ۰۳</span>
            </div>

            <div className="lobby-actions-grid">
              <button
                type="button"
                className="button-primary solo-action"
                onClick={() => onStartSoloAi(selectedMode, aiDifficulty, turnTime)}
              >
                <span className="action-icon">♟</span>
                <span><b>بازی سریع با هوش مصنوعی</b><small>شروع فوری یک مسابقه‌ی تازه</small></span>
                <i>←</i>
              </button>
              <button
                type="button"
                className="button-secondary private-action"
                onClick={() => onCreateRoom(selectedMode, turnTime, isRanked)}
              >
                <span className="action-icon">♛</span>
                <span><b>ساخت اتاق خصوصی</b><small>دعوت دوستان با کد اختصاصی</small></span>
                <i>←</i>
              </button>
            </div>

            <form className="join-room-form" onSubmit={submitJoin}>
              <div className="join-room-copy">
                <b>کد دعوت داری؟</b>
                <small>به میز دوستت بپیوند.</small>
              </div>
              <input
                value={inviteCodeInput}
                onChange={(event) => setInviteCodeInput(event.target.value.replace(/[^a-z0-9]/gi, '').slice(0, 6).toUpperCase())}
                placeholder="کد ۶ رقمی"
                maxLength={6}
                minLength={6}
                dir="ltr"
                autoComplete="off"
                aria-label="کد دعوت اتاق"
              />
              <button type="submit" className="button-secondary" disabled={inviteCodeInput.length !== 6}>ورود</button>
            </form>
          </section>
        </>
      )}
    </div>
  );
};
