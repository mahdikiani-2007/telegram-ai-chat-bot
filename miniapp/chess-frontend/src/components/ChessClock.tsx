import React from 'react';

interface ChessClockProps {
  remainingMs: number;
  isActive: boolean;
}

export const ChessClock: React.FC<ChessClockProps> = ({ remainingMs, isActive }) => {
  const totalSeconds = Math.max(0, Math.floor(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;

  const isLowTime = remainingMs < 30000 && remainingMs > 0;
  const isCritical = remainingMs < 10000 && remainingMs > 0;

  let displayTime = '';
  if (isCritical) {
    const tenths = Math.floor((remainingMs % 1000) / 100);
    displayTime = `${seconds}.${tenths}`;
  } else {
    displayTime = `${minutes}:${seconds.toString().padStart(2, '0')}`;
  }

  return (
    <div
      className={`clock-box ${isActive ? 'active' : ''} ${isLowTime ? 'low-time' : ''}`}
    >
      ⏱ {displayTime}
    </div>
  );
};
