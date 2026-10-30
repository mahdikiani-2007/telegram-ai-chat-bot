import React from 'react';

interface GameControlsProps {
  onResignClick: () => void;
  onDrawClick: () => void;
  onToggleHistory: () => void;
  drawOfferedByMe: boolean;
  isDrawIncoming: boolean;
  onAcceptDraw: () => void;
  onDeclineDraw: () => void;
  isGameOver: boolean;
  onBackToLobby: () => void;
}

export const GameControls: React.FC<GameControlsProps> = ({
  onResignClick,
  onDrawClick,
  onToggleHistory,
  drawOfferedByMe,
  isDrawIncoming,
  onAcceptDraw,
  onDeclineDraw,
  isGameOver,
  onBackToLobby,
}) => {
  if (isGameOver) {
    return (
      <div className="game-controls">
        <button className="btn-primary" onClick={onBackToLobby}>
          ↩ بازگشت به لابی
        </button>
      </div>
    );
  }

  if (isDrawIncoming) {
    return (
      <div className="game-controls" style={{ background: 'rgba(251, 191, 36, 0.1)', padding: 10, borderRadius: 12 }}>
        <span style={{ fontSize: 13, alignSelf: 'center', fontWeight: 800 }}>🤝 پیشنهاد تساوی از حریف:</span>
        <button className="btn-control" style={{ background: 'var(--lime)', color: '#000' }} onClick={onAcceptDraw}>
          قبول ✅
        </button>
        <button className="btn-control danger" onClick={onDeclineDraw}>
          رد ❌
        </button>
      </div>
    );
  }

  return (
    <div className="game-controls">
      <button className="btn-control danger" onClick={onResignClick}>
        🏳️ تسلیم
      </button>

      <button
        className="btn-control"
        onClick={onDrawClick}
        disabled={drawOfferedByMe}
        style={{ opacity: drawOfferedByMe ? 0.6 : 1 }}
      >
        🤝 {drawOfferedByMe ? 'پیشنهاد ارسال شد' : 'پیشنهاد تساوی'}
      </button>

      <button className="btn-control" onClick={onToggleHistory}>
        📜 حرکات
      </button>
    </div>
  );
};
