import React from 'react';

export interface CardProps {
  code: string;
  isPlayable?: boolean;
  isSelected?: boolean;
  isDisabled?: boolean;
  isDragging?: boolean;
  onClick?: () => void;
  onPointerDown?: (event: React.PointerEvent<HTMLDivElement>) => void;
  style?: React.CSSProperties;
  className?: string;
}

const SUIT_SYMBOLS: Record<string, string> = {
  S: '♠',
  H: '♥',
  D: '♦',
  C: '♣',
};

const PIP_LAYOUT: Record<number, Array<[number, number]>> = {
  1: [[50, 50]],
  2: [[50, 27], [50, 73]],
  3: [[50, 24], [50, 50], [50, 76]],
  4: [[34, 28], [66, 28], [34, 72], [66, 72]],
  5: [[34, 25], [66, 25], [50, 50], [34, 75], [66, 75]],
  6: [[34, 23], [66, 23], [34, 50], [66, 50], [34, 77], [66, 77]],
  7: [[34, 21], [66, 21], [34, 44], [66, 44], [50, 56], [34, 79], [66, 79]],
  8: [[34, 20], [66, 20], [34, 42], [66, 42], [34, 58], [66, 58], [34, 80], [66, 80]],
  9: [[34, 18], [66, 18], [34, 38], [66, 38], [50, 50], [34, 62], [66, 62], [34, 82], [66, 82]],
  10: [[34, 16], [66, 16], [34, 35], [66, 35], [34, 50], [66, 50], [34, 65], [66, 65], [34, 84], [66, 84]],
};

const CardBack: React.FC = () => (
  <svg className="card-back-art" viewBox="0 0 100 140" aria-hidden="true">
    <defs>
      <pattern id="hokm-card-guilloche" width="16" height="16" patternUnits="userSpaceOnUse">
        <path d="M8 0 16 8 8 16 0 8Z M8 3 13 8 8 13 3 8Z" fill="none" stroke="#e6c879" strokeWidth=".65" />
        <circle cx="8" cy="8" r="1.8" fill="#e6c879" />
      </pattern>
    </defs>
    <rect x="3" y="3" width="94" height="134" rx="7" fill="#102d32" />
    <rect x="6" y="6" width="88" height="128" rx="5" fill="url(#hokm-card-guilloche)" />
    <rect x="10" y="10" width="80" height="120" rx="4" fill="none" stroke="#f5dfa0" strokeWidth="1.5" />
    <rect x="14" y="14" width="72" height="112" rx="3" fill="none" stroke="#b98d3a" strokeWidth=".8" />
    <path d="M50 30 68 48 50 66 32 48Z" fill="#e2c176" stroke="#fff0be" strokeWidth="1.2" />
    <path d="M50 37 61 48 50 59 39 48Z" fill="#143e3a" stroke="#9c7a37" />
    <path d="M50 44 54 48 50 52 46 48Z" fill="#f7e5ae" />
    <path d="M50 75c-15 0-21 12-21 20 0 10 10 17 21 17s21-7 21-17c0-8-6-20-21-20Z" fill="#173e3a" stroke="#d7b968" strokeWidth="1.2" />
    <text x="50" y="101" textAnchor="middle" fontSize="20" fill="#f2d891">♛</text>
  </svg>
);

const CourtArt: React.FC<{ rank: string; symbol: string; red: boolean }> = ({ rank, symbol, red }) => {
  const ink = red ? '#a51f32' : '#172b35';
  const accent = red ? '#dca45f' : '#c49a48';
  return (
    <svg className="court-art" viewBox="0 0 100 112" aria-hidden="true">
      <rect x="2" y="2" width="96" height="108" rx="7" fill="#f8f0d9" stroke={accent} strokeWidth="1.5" />
      <rect x="6" y="6" width="88" height="100" rx="5" fill="none" stroke={ink} strokeOpacity=".45" />
      <path d="M10 30 50 8 90 30 50 50Z" fill={accent} opacity=".22" />
      <path d="M50 9 58 21 50 33 42 21Z" fill={accent} />
      <circle cx="50" cy="40" r="19" fill="#e7cf9d" stroke={ink} strokeWidth="1.8" />
      <path d="M33 37 38 20 46 29 50 17 56 29 64 20 68 37Z" fill={accent} stroke={ink} strokeWidth="1.5" />
      <path d="M36 40 Q50 32 64 40 L68 62 Q50 72 32 62Z" fill="#e6d6b2" stroke={ink} strokeWidth="1.4" />
      <path d="M32 64 18 90 50 101 82 90 68 64 58 73 50 66 42 73Z" fill={ink} />
      <path d="M41 70 50 80 59 70 64 98 50 105 36 98Z" fill={red ? '#9e2434' : '#1d5960'} stroke={accent} strokeWidth="1.6" />
      <path d="M15 89 35 75 40 103 20 98Z M85 89 65 75 60 103 80 98Z" fill={accent} opacity=".8" />
      <path d="M16 24 23 31 16 38 9 31Z M84 24 91 31 84 38 77 31Z" fill={ink} opacity=".75" />
      <text x="50" y="94" textAnchor="middle" fontSize="14" fontWeight="900" fill="#f9e8bd">{rank}</text>
      <text x="50" y="17" textAnchor="middle" fontSize="8" fill={ink}>{symbol}</text>
      <g transform="translate(100 112) rotate(180)">
        <path d="M10 30 50 8 90 30 50 50Z" fill={accent} opacity=".22" />
        <path d="M50 9 58 21 50 33 42 21Z" fill={accent} />
        <circle cx="50" cy="40" r="19" fill="#e7cf9d" stroke={ink} strokeWidth="1.8" />
        <path d="M33 37 38 20 46 29 50 17 56 29 64 20 68 37Z" fill={accent} stroke={ink} strokeWidth="1.5" />
        <path d="M36 40 Q50 32 64 40 L68 62 Q50 72 32 62Z" fill="#e6d6b2" stroke={ink} strokeWidth="1.4" />
        <path d="M32 64 18 90 50 101 82 90 68 64 58 73 50 66 42 73Z" fill={ink} />
        <path d="M41 70 50 80 59 70 64 98 50 105 36 98Z" fill={red ? '#9e2434' : '#1d5960'} stroke={accent} strokeWidth="1.6" />
      </g>
    </svg>
  );
};

export const PlayingCard: React.FC<CardProps> = ({
  code,
  isPlayable = true,
  isSelected = false,
  isDisabled = false,
  isDragging = false,
  onClick,
  onPointerDown,
  style,
  className = '',
}) => {
  if (!code || code === 'BACK') {
    return (
      <div className={'playing-card card-back ' + className} style={style} onClick={onClick} aria-hidden="true">
        <CardBack />
      </div>
    );
  }

  const suit = code.slice(-1).toUpperCase();
  const rank = code.slice(0, -1).toUpperCase();
  const red = suit === 'H' || suit === 'D';
  const symbol = SUIT_SYMBOLS[suit] || suit;
  const numericRank = Number.parseInt(rank, 10);
  const isCourt = ['J', 'Q', 'K'].includes(rank);
  const pipCount = rank === 'A' ? 1 : numericRank;
  const pips = PIP_LAYOUT[pipCount] || [];

  const classes = [
    'playing-card',
    'card-face',
    red ? 'red' : 'black',
    isDisabled ? 'disabled' : isPlayable ? 'playable' : 'static',
    isSelected ? 'selected' : '',
    isDragging ? 'dragging-source' : '',
    className,
  ].filter(Boolean).join(' ');

  return (
    <div
      className={classes}
      style={style}
      onClick={isPlayable ? onClick : undefined}
      onPointerDown={isPlayable ? onPointerDown : undefined}
      role={isPlayable ? 'button' : undefined}
      aria-label={rank + ' ' + symbol}
      aria-disabled={isDisabled || !isPlayable}
      tabIndex={isPlayable ? 0 : -1}
    >
      <div className="card-inner-frame" aria-hidden="true" />
      <div className="card-corner card-corner-top">
        <span className="corner-rank">{rank}</span>
        <span className="corner-suit">{symbol}</span>
      </div>
      <div className={'card-center-art ' + (isCourt ? 'is-court' : '')} aria-hidden="true">
        {isCourt ? (
          <CourtArt rank={rank} symbol={symbol} red={red} />
        ) : (
          <svg className={'pip-art ' + (rank === 'A' ? 'ace-art' : '')} viewBox="0 0 100 100">
            {pips.map(([x, y], index) => (
              <text
                key={String(x) + '-' + String(y) + '-' + String(index)}
                x={x}
                y={y}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={rank === 'A' ? 48 : pipCount >= 8 ? 17 : 21}
                fontWeight="700"
                transform={index % 2 ? 'rotate(180 ' + x + ' ' + y + ')' : undefined}
              >
                {symbol}
              </text>
            ))}
            {rank === 'A' && <circle cx="50" cy="50" r="34" fill="none" stroke="currentColor" strokeOpacity=".18" strokeWidth="1.5" />}
          </svg>
        )}
      </div>
      <div className="card-corner card-corner-bottom">
        <span className="corner-rank">{rank}</span>
        <span className="corner-suit">{symbol}</span>
      </div>
      <span className="card-foil-glint" aria-hidden="true" />
    </div>
  );
};
