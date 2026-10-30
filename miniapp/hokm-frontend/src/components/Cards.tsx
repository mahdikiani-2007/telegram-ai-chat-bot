import React from 'react';

export interface CardProps {
  code: string;
  isPlayable?: boolean;
  isSelected?: boolean;
  onClick?: () => void;
  onPointerDown?: (e: React.PointerEvent) => void;
  style?: React.CSSProperties;
}

const SUIT_SYMBOLS: Record<string, string> = {
  S: '♠',
  H: '♥',
  D: '♦',
  C: '♣',
};

export const PlayingCard: React.FC<CardProps> = ({
  code,
  isPlayable = true,
  isSelected = false,
  onClick,
  onPointerDown,
  style,
}) => {
  if (!code || code === 'BACK') {
    return (
      <div className="playing-card card-back" style={style} onClick={onClick}>
        <div style={{ width: '100%', height: '100%', borderRadius: 4, border: '1px dashed rgba(255,255,255,0.2)' }} />
      </div>
    );
  }

  const suitChar = code.slice(-1).toUpperCase();
  const rankStr = code.slice(0, -1).toUpperCase();
  const isRed = suitChar === 'H' || suitChar === 'D';
  const symbol = SUIT_SYMBOLS[suitChar] || suitChar;

  return (
    <div
      className={`playing-card ${isRed ? 'red' : 'black'} ${
        isPlayable ? 'playable' : 'disabled'
      } ${isSelected ? 'selected' : ''}`}
      style={style}
      onClick={isPlayable ? onClick : undefined}
      onPointerDown={isPlayable ? onPointerDown : undefined}
    >
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: 'fit-content' }}>
        <span className="corner-rank">{rankStr}</span>
        <span className="corner-suit">{symbol}</span>
      </div>

      <div className="center-suit">{symbol}</div>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          width: 'fit-content',
          transform: 'rotate(180deg)',
          alignSelf: 'flex-end',
        }}
      >
        <span className="corner-rank">{rankStr}</span>
        <span className="corner-suit">{symbol}</span>
      </div>
    </div>
  );
};
