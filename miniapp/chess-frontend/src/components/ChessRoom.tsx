import React, { useEffect, useState } from 'react';
import { getRoomInfo } from '../services/api';
import { chessSocket } from '../services/websocket';
import { getTelegram, hapticSuccess, hapticTap } from '../services/telegram';

interface ChessRoomProps {
  roomId: string;
  inviteCode: string;
  onGameStart: () => void;
  onCancel: () => void;
}

export const ChessRoom: React.FC<ChessRoomProps> = ({
  roomId,
  inviteCode,
  onGameStart,
  onCancel,
}) => {
  const [copied, setCopied] = useState(false);

  // Derive invite URL
  const inviteUrl = `${window.location.origin}/chess/?invite=${inviteCode}`;

  useEffect(() => {
    // 1. WebSocket listener for game.start
    const unsubMsg = chessSocket.onMessage((msg) => {
      if (msg.type === 'game.start' && msg.game_id === roomId) {
        hapticSuccess();
        onGameStart();
      }
    });

    // 2. Fallback polling check every 3s in case socket missed it
    const interval = window.setInterval(async () => {
      try {
        const info = await getRoomInfo(roomId);
        if (info.ok && info.status === 'active') {
          hapticSuccess();
          onGameStart();
        }
      } catch {}
    }, 3000);

    return () => {
      unsubMsg();
      clearInterval(interval);
    };
  }, [roomId, onGameStart]);

  const handleCopy = () => {
    navigator.clipboard.writeText(inviteUrl);
    setCopied(true);
    hapticTap('light');
    setTimeout(() => setCopied(false), 2500);
  };

  const handleShareTelegram = () => {
    hapticTap('medium');
    const tg = getTelegram();
    const shareText = `♟️ بیا با من شطرنج بازی کن! کلیک کن تا وارد بازی بشی:`;
    const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(inviteUrl)}&text=${encodeURIComponent(shareText)}`;

    if (tg?.openTelegramLink) {
      tg.openTelegramLink(shareUrl);
    } else {
      window.open(shareUrl, '_blank');
    }
  };

  return (
    <div className="glass-panel" style={{ textAlign: 'center', padding: '28px 18px' }}>
      <div style={{ fontSize: 44, marginBottom: 12 }}>⏳</div>
      <h2 style={{ fontSize: 20, fontWeight: 900, marginBottom: 6 }}>اتاق خصوصی ایجاد شد</h2>
      <p style={{ fontSize: 13, color: 'var(--dim)', lineHeight: 1.8, marginBottom: 20 }}>
        این لینک یا کد را برای دوستت در تلگرام بفرست تا مسابقه شروع شود.
      </p>

      {/* Code Display */}
      <div
        style={{
          background: 'var(--panel)',
          border: '1px dashed var(--cyan)',
          borderRadius: 14,
          padding: '12px 16px',
          marginBottom: 18,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <span style={{ fontSize: 13, color: 'var(--dim)' }}>کد دعوت:</span>
        <span style={{ fontSize: 18, fontWeight: 900, fontFamily: 'monospace', color: 'var(--cyan)' }}>
          {inviteCode}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <button className="btn-primary" onClick={handleShareTelegram}>
          ✈️ ارسال دعوت در تلگرام
        </button>

        <button className="btn-secondary" onClick={handleCopy}>
          {copied ? '✅ لینک کپی شد!' : '📋 کپی لینک دعوت'}
        </button>

        <div style={{ margin: '18px 0 8px' }}>
          <div className="spinner" style={{ width: 28, height: 28 }} />
          <div style={{ fontSize: 12, color: 'var(--dim)', marginTop: 8 }}>در انتظار ورود حریف...</div>
        </div>

        <button
          className="btn-control danger"
          style={{ width: '100%', marginTop: 6 }}
          onClick={onCancel}
        >
          انصراف و خروج
        </button>
      </div>
    </div>
  );
};
