import React, { useEffect, useState } from 'react';
import { PlayerStats, TIME_CONTROLS } from '../types';
import { ApiError, cancelMatchmaking, createBotGame, createRoom, getProfile, joinRoom, matchmake } from '../services/api';
import { hapticError, hapticSuccess, hapticTap } from '../services/telegram';

interface ChessLobbyProps {
  onGameCreated: (roomId: string, inviteCode: string) => void;
  onGameJoined: (roomId: string) => void;
  activeRoomId: string | null;
}

export const ChessLobby: React.FC<ChessLobbyProps> = ({
  onGameCreated,
  onGameJoined,
  activeRoomId,
}) => {
  const [selectedTc, setSelectedTc] = useState<string>('blitz_5_0');
  const [profile, setProfile] = useState<{
    name: string;
    stats: PlayerStats;
    active_room: string | null;
  } | null>(null);
  const [botDifficulty, setBotDifficulty] = useState<'easy' | 'medium' | 'hard'>('medium');
  const [isMatching, setIsMatching] = useState(false);
  const [inviteInput, setInviteInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    getProfile()
      .then((res) => {
        setProfile({
          name: res.name,
          stats: res.stats,
          active_room: res.active_room,
        });
      })
      .catch((e) => console.debug('Profile load failed', e));
  }, [activeRoomId]);

  const resumableRoomId = activeRoomId || profile?.active_room || null;

  const resumeExistingGame = (error: unknown): boolean => {
    if (error instanceof ApiError && error.code === 'already_in_game' && error.roomId) {
      setIsMatching(false);
      hapticSuccess();
      onGameJoined(error.roomId);
      return true;
    }
    return false;
  };

  const handlePlayBot = async () => {
    setLoading(true);
    setErrorMessage(null);
    hapticTap('medium');
    try {
      const res = await createBotGame(botDifficulty, selectedTc);
      hapticSuccess();
      onGameJoined(res.room_id);
    } catch (err: any) {
      if (!resumeExistingGame(err)) {
        setErrorMessage(err.message || 'خطا در شروع بازی با ربات');
        hapticError();
      }
    } finally {
      setLoading(false);
    }
  };

  // Polling for matchmaking while queued
  useEffect(() => {
    if (!isMatching) return;

    const interval = window.setInterval(async () => {
      try {
        const res = await matchmake(selectedTc);
        if (res.status === 'matched' && res.room_id) {
          setIsMatching(false);
          hapticSuccess();
          onGameJoined(res.room_id);
        }
      } catch (err: any) {
        setIsMatching(false);
        if (!resumeExistingGame(err)) {
          setErrorMessage(err.message || 'خطا در صف همتایابی');
          hapticError();
        }
      }
    }, 2500);

    return () => clearInterval(interval);
  }, [isMatching, selectedTc, onGameJoined]);

  const handleCreatePrivate = async () => {
    setLoading(true);
    setErrorMessage(null);
    hapticTap('medium');
    try {
      const res = await createRoom(selectedTc);
      hapticSuccess();
      onGameCreated(res.room_id, res.invite_code);
    } catch (err: any) {
      if (!resumeExistingGame(err)) {
        setErrorMessage(err.message || 'خطا در ایجاد اتاق');
        hapticError();
      }
    } finally {
      setLoading(false);
    }
  };

  const handleStartMatchmaking = async () => {
    setLoading(true);
    setErrorMessage(null);
    hapticTap('medium');
    try {
      const res = await matchmake(selectedTc);
      if (res.status === 'matched' && res.room_id) {
        hapticSuccess();
        onGameJoined(res.room_id);
      } else {
        setIsMatching(true);
      }
    } catch (err: any) {
      if (!resumeExistingGame(err)) {
        setErrorMessage(err.message || 'خطا در شروع بازی تصادفی');
        hapticError();
      }
    } finally {
      setLoading(false);
    }
  };

  const handleCancelMatchmaking = async () => {
    try {
      await cancelMatchmaking();
    } catch {}
    setIsMatching(false);
    hapticTap('light');
  };

  const handleJoinByCode = async () => {
    if (!inviteInput.trim()) return;
    setLoading(true);
    setErrorMessage(null);
    hapticTap('medium');
    try {
      const code = inviteInput.trim();
      const res = await joinRoom(undefined, code);
      hapticSuccess();
      onGameJoined(res.room_id);
    } catch (err: any) {
      setErrorMessage(err.message || 'کد دعوت نامعتبر است');
      hapticError();
    } finally {
      setLoading(false);
    }
  };

  const stats = profile?.stats || { wins: 0, losses: 0, draws: 0, games_played: 0 };
  const winRate = stats.games_played > 0 ? Math.round((stats.wins / stats.games_played) * 100) : 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* Resume banner if player has active match */}
      {resumableRoomId && (
        <div
          className="glass-panel"
          style={{
            borderColor: 'var(--lime)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div>
            <div style={{ fontWeight: 800, fontSize: 14, color: 'var(--lime)' }}>⚡ بازی در حال اجرا داری!</div>
            <div style={{ fontSize: 12, color: 'var(--dim)', marginTop: 2 }}>می‌تونی دوباره به بازی برگردی</div>
          </div>
          <button
            className="btn-primary"
            style={{ width: 'auto', padding: '8px 16px', fontSize: 13 }}
            onClick={() => onGameJoined(resumableRoomId)}
          >
            ادامه بازی
          </button>
        </div>
      )}

      {/* Profile Card */}
      <div className="glass-panel">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 16, fontWeight: 900 }}>{profile?.name || 'بازیکن شطرنج'}</div>
            <div style={{ fontSize: 12, color: 'var(--dim)', marginTop: 2 }}>پروفایل رقابتی</div>
          </div>
          <div className="chip-status">⚔️ آنلاین</div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, marginTop: 14 }}>
          <div style={{ background: 'var(--panel)', padding: '10px 4px', borderRadius: 10, textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 900, color: 'var(--lime)' }}>{stats.wins}</div>
            <div style={{ fontSize: 11, color: 'var(--dim)' }}>برد</div>
          </div>
          <div style={{ background: 'var(--panel)', padding: '10px 4px', borderRadius: 10, textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 900, color: 'var(--red)' }}>{stats.losses}</div>
            <div style={{ fontSize: 11, color: 'var(--dim)' }}>باخت</div>
          </div>
          <div style={{ background: 'var(--panel)', padding: '10px 4px', borderRadius: 10, textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 900, color: 'var(--amber)' }}>{stats.draws}</div>
            <div style={{ fontSize: 11, color: 'var(--dim)' }}>تساوی</div>
          </div>
          <div style={{ background: 'var(--panel)', padding: '10px 4px', borderRadius: 10, textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 900, color: 'var(--cyan)' }}>%{winRate}</div>
            <div style={{ fontSize: 11, color: 'var(--dim)' }}>درصد برد</div>
          </div>
        </div>
      </div>

      {/* Time Control Selection */}
      <div className="glass-panel">
        <div style={{ fontSize: 14, fontWeight: 800, marginBottom: 4 }}>⏱ انتخاب کنترل زمان</div>
        <div className="tc-chips-grid">
          {TIME_CONTROLS.map((tc) => (
            <div
              key={tc.key}
              className={`tc-chip ${selectedTc === tc.key ? 'active' : ''}`}
              onClick={() => {
                setSelectedTc(tc.key);
                hapticTap('light');
              }}
            >
              {tc.label}
            </div>
          ))}
        </div>

        {errorMessage && (
          <div style={{ color: 'var(--red)', fontSize: 13, marginBottom: 12, textAlign: 'center' }}>
            ⚠️ {errorMessage}
          </div>
        )}

        {/* Action Buttons */}
        {isMatching ? (
          <div style={{ textAlign: 'center', padding: '10px 0' }}>
            <div className="spinner" style={{ marginBottom: 12 }} />
            <div style={{ fontSize: 14, fontWeight: 800 }}>در حال جستجوی حریف تصادفی...</div>
            <div style={{ fontSize: 12, color: 'var(--dim)', margin: '4px 0 14px' }}>
              کنترل زمان: {TIME_CONTROLS.find((t) => t.key === selectedTc)?.label}
            </div>
            <button className="btn-secondary" onClick={handleCancelMatchmaking}>
              لغو جستجو
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <button className="btn-primary" onClick={handleStartMatchmaking} disabled={loading}>
              🎲 حریف تصادفی آنلاین
            </button>

            <button className="btn-secondary" onClick={handleCreatePrivate} disabled={loading}>
              👥 ایجاد بازی خصوصی با دوستان
            </button>
          </div>
        )}
      </div>

      {/* Play against AI */}
      <div className="glass-panel">
        <div style={{ fontSize: 15, fontWeight: 900, marginBottom: 5 }}>🤖 بازی با ربات</div>
        <div style={{ fontSize: 12, color: 'var(--dim)', lineHeight: 1.8, marginBottom: 12 }}>
          سطح دشواری ربات را انتخاب کن و بدون نیاز به حریف آنلاین بازی را شروع کن.
        </div>

        <div style={{ fontSize: 12, color: 'var(--dim)', marginBottom: 7 }}>سطح ربات</div>
        <div className="tc-chips-grid" style={{ marginBottom: 12 }}>
          {([
            { key: 'easy', label: 'مبتدی' },
            { key: 'medium', label: 'متوسط' },
            { key: 'hard', label: 'حرفه‌ای' },
          ] as const).map((level) => (
            <div
              key={level.key}
              className={`tc-chip ${botDifficulty === level.key ? 'active' : ''}`}
              onClick={() => {
                setBotDifficulty(level.key);
                hapticTap('light');
              }}
            >
              {level.label}
            </div>
          ))}
        </div>

        <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 12 }}>
          کنترل زمان: {TIME_CONTROLS.find((tc) => tc.key === selectedTc)?.label}
        </div>

        <button className="btn-primary" style={{ width: '100%' }} onClick={handlePlayBot} disabled={loading}>
          {loading ? 'در حال آماده‌سازی...' : '♟️ شروع بازی با ربات'}
        </button>
      </div>

      {/* Join with Invite Code */}
      <div className="glass-panel">
        <div style={{ fontSize: 13, fontWeight: 800, marginBottom: 8 }}>🔗 پیوستن با کد دعوت</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="text"
            placeholder="کد دعوت یا لینک اتاق..."
            value={inviteInput}
            onChange={(e) => setInviteInput(e.target.value)}
            style={{
              flex: 1,
              background: 'var(--panel)',
              border: '1px solid var(--panel-line)',
              borderRadius: 12,
              padding: '10px 14px',
              color: 'var(--text)',
              fontFamily: 'var(--font)',
              fontSize: 13,
              direction: 'ltr',
            }}
          />
          <button
            className="btn-secondary"
            style={{ width: 'auto', padding: '10px 18px' }}
            onClick={handleJoinByCode}
            disabled={loading || !inviteInput.trim()}
          >
            ورود
          </button>
        </div>
      </div>
    </div>
  );
};
