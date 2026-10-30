import React, { useState } from 'react';

interface HokmLobbyProps {
  user: any;
  ratings: any;
  activeRoom: any;
  onCreateRoom: (mode: string, timeLimit: number, isRanked: boolean) => void;
  onStartSoloAi: (mode: string, difficulty: string, timeLimit: number) => void;
  onJoinRoom: (code: string) => void;
  onAddAi: (difficulty: string) => void;
  onStartGame: () => void;
}

export const HokmLobby: React.FC<HokmLobbyProps> = ({
  user,
  ratings,
  activeRoom,
  onCreateRoom,
  onStartSoloAi,
  onJoinRoom,
  onAddAi,
  onStartGame,
}) => {
  const [selectedMode, setSelectedMode] = useState<'hokm_4p' | 'hokm_3p' | 'hokm_2p'>('hokm_4p');
  const [turnTime, setTurnTime] = useState<number>(30);
  const [isRanked, setIsRanked] = useState<boolean>(true);
  const [inviteCodeInput, setInviteCodeInput] = useState<string>('');
  const [aiDifficulty, setAiDifficulty] = useState<string>('medium');
  const [copied, setCopied] = useState<boolean>(false);

  const modeRating = ratings?.[selectedMode] || { rating: 1000, tier: 'Bronze' };

  const handleCopyCode = () => {
    if (activeRoom?.invite_code && navigator.clipboard) {
      navigator.clipboard.writeText(activeRoom.invite_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* User Rating Header Card */}
      <div className="velvet-panel" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>بازیکن</div>
          <div style={{ fontSize: 18, fontWeight: 900, color: 'var(--gold-light)' }}>
            👑 {user?.name || 'مهمان'}
          </div>
        </div>

        <div style={{ textAlign: 'left' }}>
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>لیگ و ریتینگ</div>
          <div
            style={{
              background: 'rgba(234, 179, 8, 0.15)',
              border: '1px solid var(--gold)',
              borderRadius: 8,
              padding: '2px 8px',
              fontSize: 13,
              fontWeight: 800,
              color: 'var(--gold)',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
            }}
          >
            🛡️ {modeRating.tier} ({modeRating.rating})
          </div>
        </div>
      </div>

      {/* If currently in a waiting room, show Room Waiting View */}
      {activeRoom && activeRoom.stage === 'waiting' ? (
        <div className="velvet-panel">
          <div style={{ textAlign: 'center', marginBottom: 14 }}>
            <h3 style={{ color: 'var(--gold)', fontSize: 18, marginBottom: 4 }}>اتاق انتظار بازی</h3>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, marginTop: 6 }}>
              <span style={{ color: 'var(--text-dim)', fontSize: 13 }}>کد دعوت:</span>
              <b style={{ color: '#ffffff', letterSpacing: 3, fontSize: 18, background: 'rgba(0,0,0,0.4)', padding: '2px 8px', borderRadius: 6 }}>
                {activeRoom.invite_code}
              </b>
              <button
                className="outline-btn"
                style={{ padding: '4px 8px', fontSize: 12 }}
                onClick={handleCopyCode}
              >
                {copied ? '✅ کپی شد' : '📋 کپی'}
              </button>
            </div>
          </div>

          {/* Seated Players List */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
            {activeRoom.players?.map((p: any, idx: number) => {
              if (!p) {
                return (
                  <div
                    key={idx}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      background: 'rgba(0,0,0,0.2)',
                      padding: '8px 12px',
                      borderRadius: 10,
                      border: '1px dashed rgba(255,255,255,0.15)',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, opacity: 0.6 }}>
                      <span>🪑</span>
                      <span style={{ fontSize: 13 }}>جایگاه {idx + 1} (خالی)</span>
                    </div>
                    <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>منتظر بازیکن یا ربات</span>
                  </div>
                );
              }

              return (
                <div
                  key={idx}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    background: 'rgba(0,0,0,0.3)',
                    padding: '8px 12px',
                    borderRadius: 10,
                    border: '1px solid rgba(255,255,255,0.08)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span>{p.is_ai ? '🤖' : '👤'}</span>
                    <span style={{ fontWeight: 700, fontSize: 14 }}>{p.name}</span>
                  </div>
                  <span style={{ fontSize: 12, color: 'var(--gold-light)' }}>
                    جایگاه {idx + 1} {p.team !== null && p.team !== undefined && `(تیم ${p.team === 0 ? 'الف' : 'ب'})`}
                  </span>
                </div>
              );
            })}
          </div>

          {/* Add AI Controls */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
            <select
              value={aiDifficulty}
              onChange={(e) => setAiDifficulty(e.target.value)}
              style={{
                flex: 1,
                background: 'rgba(0,0,0,0.4)',
                color: '#fff',
                border: '1px solid var(--panel-border)',
                borderRadius: 10,
                padding: '8px 10px',
                fontFamily: 'Vazirmatn',
              }}
            >
              <option value="easy">ربات آسان</option>
              <option value="medium">ربات متوسط</option>
              <option value="hard">ربات حرفه‌ای</option>
            </select>
            <button className="outline-btn" onClick={() => onAddAi(aiDifficulty)}>
              + افزودن ربات
            </button>
          </div>

          <button className="gold-btn" style={{ width: '100%', padding: '14px' }} onClick={onStartGame}>
            🚀 شروع مسابقه
          </button>
        </div>
      ) : (
        /* Normal Lobby Mode Selection */
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* Mode Selector */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
            {[
              { id: 'hokm_4p', label: '۴ نفره تیمی', desc: 'سنتی' },
              { id: 'hokm_3p', label: '۳ نفره', desc: '۵۱ برگی' },
              { id: 'hokm_2p', label: '۲ نفره', desc: 'درفت کارت' },
            ].map((m) => (
              <button
                key={m.id}
                onClick={() => setSelectedMode(m.id as any)}
                style={{
                  background: selectedMode === m.id ? 'var(--felt-light)' : 'rgba(255,255,255,0.04)',
                  border: `2px solid ${selectedMode === m.id ? 'var(--gold)' : 'var(--panel-border)'}`,
                  borderRadius: 14,
                  padding: '12px 8px',
                  color: selectedMode === m.id ? '#ffffff' : 'var(--text-dim)',
                  cursor: 'pointer',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 4,
                  transition: 'all 0.15s ease',
                }}
              >
                <span style={{ fontWeight: 800, fontSize: 14, fontFamily: 'Vazirmatn' }}>{m.label}</span>
                <span style={{ fontSize: 11, opacity: 0.7 }}>{m.desc}</span>
              </button>
            ))}
          </div>

          {/* AI Difficulty Selector */}
          <div className="velvet-panel" style={{ padding: 12 }}>
            <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>درجه سختی هوش مصنوعی:</div>
            <div style={{ display: 'flex', gap: 8 }}>
              {[
                { key: 'easy', label: 'آسان' },
                { key: 'medium', label: 'متوسط' },
                { key: 'hard', label: 'حرفه‌ای' },
              ].map((d) => (
                <button
                  key={d.key}
                  onClick={() => setAiDifficulty(d.key)}
                  style={{
                    flex: 1,
                    background: aiDifficulty === d.key ? 'var(--gold)' : 'rgba(255,255,255,0.06)',
                    color: aiDifficulty === d.key ? '#422006' : '#fff',
                    border: 'none',
                    borderRadius: 8,
                    padding: '8px',
                    fontSize: 13,
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>

          {/* Time Limit Selector */}
          <div className="velvet-panel" style={{ padding: 12 }}>
            <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>زمان هر حرکت:</div>
            <div style={{ display: 'flex', gap: 6, justifyContent: 'space-between' }}>
              {[15, 30, 45, 60, 0].map((t) => (
                <button
                  key={t}
                  onClick={() => setTurnTime(t)}
                  style={{
                    background: turnTime === t ? 'var(--gold)' : 'rgba(255,255,255,0.06)',
                    color: turnTime === t ? '#422006' : '#fff',
                    border: 'none',
                    borderRadius: 8,
                    padding: '6px 12px',
                    fontSize: 12,
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  {t === 0 ? 'نامحدود' : `${t}ث`}
                </button>
              ))}
            </div>
          </div>

          {/* Action Button 1: Solo AI Game (Instant start!) */}
          <button
            className="gold-btn"
            style={{ width: '100%', padding: '14px', fontSize: 16 }}
            onClick={() => onStartSoloAi(selectedMode, aiDifficulty, turnTime)}
          >
            🤖 بازی سریع با هوش مصنوعی (شروع فوری)
          </button>

          {/* Action Button 2: Private Room with Friends */}
          <button
            className="outline-btn"
            style={{ width: '100%', padding: '12px', borderColor: 'var(--gold)', color: 'var(--gold-light)' }}
            onClick={() => onCreateRoom(selectedMode, turnTime, isRanked)}
          >
            👑 ایجاد اتاق خصوصی و دعوت دوستان
          </button>

          {/* Join By Code */}
          <div className="velvet-panel" style={{ display: 'flex', gap: 8, padding: 10 }}>
            <input
              type="text"
              placeholder="کد دعوت ۶ حرفی..."
              value={inviteCodeInput}
              onChange={(e) => setInviteCodeInput(e.target.value.toUpperCase())}
              style={{
                flex: 1,
                background: 'rgba(0,0,0,0.3)',
                border: '1px solid var(--panel-border)',
                borderRadius: 10,
                color: '#fff',
                padding: '10px 14px',
                fontSize: 14,
                fontFamily: 'Vazirmatn',
                textAlign: 'center',
                letterSpacing: 2,
              }}
            />
            <button
              className="outline-btn"
              onClick={() => inviteCodeInput && onJoinRoom(inviteCodeInput)}
            >
              ورود با کد
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
