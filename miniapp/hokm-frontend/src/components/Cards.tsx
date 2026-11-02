import React from 'react';

export interface CardProps {
  code: string;
  isPlayable?: boolean;
  isSelected?: boolean;
  isDisabled?: boolean;
  isDragging?: boolean;
  onClick?: () => void;
  onPointerDown?: (event: React.PointerEvent<HTMLButtonElement>) => void;
  onKeyDown?: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
  style?: React.CSSProperties;
  className?: string;
}

const SUITS: Record<string, { symbol: string; name: string; red: boolean }> = {
  S: { symbol: '♠', name: 'پیک', red: false },
  H: { symbol: '♥', name: 'دل', red: true },
  D: { symbol: '♦', name: 'خشت', red: true },
  C: { symbol: '♣', name: 'گشنیز', red: false },
};

const PIPS: Record<number, Array<[number, number]>> = {
  1: [[50, 50]],
  2: [[50, 27], [50, 73]],
  3: [[50, 24], [50, 50], [50, 76]],
  4: [[34, 27], [66, 27], [34, 73], [66, 73]],
  5: [[34, 24], [66, 24], [50, 50], [34, 76], [66, 76]],
  6: [[34, 22], [66, 22], [34, 50], [66, 50], [34, 78], [66, 78]],
  7: [[34, 19], [66, 19], [34, 42], [66, 42], [50, 57], [34, 81], [66, 81]],
  8: [[34, 18], [66, 18], [34, 40], [66, 40], [34, 60], [66, 60], [34, 82], [66, 82]],
  9: [[34, 17], [66, 17], [34, 36], [66, 36], [50, 50], [34, 64], [66, 64], [34, 83], [66, 83]],
  10: [[34, 15], [66, 15], [34, 33], [66, 33], [34, 50], [66, 50], [34, 67], [66, 67], [34, 85], [66, 85]],
};

const CardBack: React.FC = () => {
  const patternId = React.useId().replace(/:/g, '');
  return (
    <svg className="card-back-art" viewBox="0 0 100 140" aria-hidden="true">
      <defs>
        <pattern id={patternId} width="12" height="12" patternUnits="userSpaceOnUse">
          <path d="M6 0 12 6 6 12 0 6Z M6 2 10 6 6 10 2 6Z" fill="none" stroke="#d9bc73" strokeWidth=".55" />
          <circle cx="6" cy="6" r="1" fill="#f6df9d" />
        </pattern>
        <linearGradient id={patternId + '-foil'} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#183d35" />
          <stop offset=".5" stopColor="#0c2925" />
          <stop offset="1" stopColor="#1b4b3d" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="96" height="136" rx="7" fill="#09251f" />
      <rect x="5" y="5" width="90" height="130" rx="5" fill={'url(#' + patternId + '-foil)'} />
      <rect x="8" y="8" width="84" height="124" rx="4" fill={'url(#' + patternId + ')'} />
      <rect x="12" y="12" width="76" height="116" rx="3" fill="none" stroke="#e4c982" strokeWidth="1.15" />
      <rect x="16" y="16" width="68" height="108" rx="2" fill="none" stroke="#a9823c" strokeWidth=".65" />
      <path d="M50 29 69 48 50 67 31 48Z" fill="#d2ad60" stroke="#f7e7b5" strokeWidth="1" />
      <path d="M50 37 61 48 50 59 39 48Z" fill="#153a32" stroke="#f1d994" strokeWidth=".8" />
      <path d="M50 44 54 48 50 52 46 48Z" fill="#fff0c5" />
      <text x="50" y="105" textAnchor="middle" fill="#f4dfa2" fontSize="28" fontFamily="Georgia,serif">♛</text>
      <path d="M28 82Q50 64 72 82M28 112Q50 130 72 112" fill="none" stroke="#d6b86f" strokeWidth="1" opacity=".72" />
    </svg>
  );
};

const CourtArt: React.FC<{ rank: string; symbol: string; red: boolean }> = ({ rank, symbol, red }) => {
  const ink = red ? '#9b2636' : '#17363a';
  const gold = '#bb9144';
  const cloth = rank === 'Q' ? '#7e3153' : rank === 'K' ? '#245b4b' : '#315a78';
  return (
    <svg className="court-art" viewBox="0 0 100 116" aria-hidden="true">
      <rect x="2" y="2" width="96" height="112" rx="7" fill="#f7f0dc" stroke="#b99a5b" strokeWidth="1.3" />
      <rect x="6" y="6" width="88" height="104" rx="5" fill="none" stroke="#d2bd8d" strokeWidth=".7" />
      <path d="M12 24 50 8 88 24 50 42Z" fill="#d6b76c" opacity=".4" />
      <path d="M50 10 57 20 50 30 43 20Z" fill={gold} stroke={ink} strokeWidth="1" />
      <path d="M29 49Q31 24 50 24Q69 24 71 49L65 62H35Z" fill="#d5bd91" stroke={ink} strokeWidth="1.6" />
      <path d="M28 46Q31 18 50 18Q69 18 72 46L64 38 59 28 50 36 41 28 36 39Z" fill={rank === 'K' ? '#b78a32' : '#d8d0ba'} stroke={ink} strokeWidth="1.4" />
      <path d="M35 50Q50 41 65 50L68 66Q50 76 32 66Z" fill="#e9d7b5" stroke={ink} strokeWidth="1.4" />
      <path d="M27 67 15 105 50 111 85 105 73 67 62 77 50 69 38 77Z" fill={cloth} stroke={ink} strokeWidth="1.5" />
      <path d="M39 75 50 86 61 75 68 105 50 111 32 105Z" fill={ink} opacity=".8" />
      <path d="M50 78 56 86 50 94 44 86Z" fill="#e8cf8c" stroke={gold} strokeWidth="1" />
      <path d="M14 28 21 35 14 42 7 35ZM86 28 93 35 86 42 79 35Z" fill={ink} opacity=".6" />
      <text x="50" y="103" textAnchor="middle" fontSize="10" fontWeight="900" fill="#fff0cd">{rank}</text>
      <text x="50" y="15" textAnchor="middle" fontSize="8" fill={ink}>{symbol}</text>
      <g transform="translate(100 116) rotate(180)">
        <path d="M29 49Q31 24 50 24Q69 24 71 49L65 62H35Z" fill="#d5bd91" stroke={ink} strokeWidth="1.6" />
        <path d="M28 46Q31 18 50 18Q69 18 72 46L64 38 59 28 50 36 41 28 36 39Z" fill={rank === 'K' ? '#b78a32' : '#d8d0ba'} stroke={ink} strokeWidth="1.4" />
        <path d="M35 50Q50 41 65 50L68 66Q50 76 32 66Z" fill="#e9d7b5" stroke={ink} strokeWidth="1.4" />
        <path d="M27 67 15 105 50 111 85 105 73 67 62 77 50 69 38 77Z" fill={cloth} stroke={ink} strokeWidth="1.5" />
      </g>
    </svg>
  );
};

const PlayingCardComponent: React.FC<CardProps> = ({
  code,
  isPlayable = false,
  isSelected = false,
  isDisabled = false,
  isDragging = false,
  onClick,
  onPointerDown,
  onKeyDown,
  style,
  className = '',
}) => {
  if (!code || code === 'BACK') {
    return (
      <div className={'playing-card card-back ' + className} style={style} aria-hidden="true">
        <CardBack />
      </div>
    );
  }

  const suit = code.slice(-1).toUpperCase();
  const rank = code.slice(0, -1).toUpperCase();
  const suitInfo = SUITS[suit] || { symbol: suit, name: suit, red: false };
  const pipCount = rank === 'A' ? 1 : Number.parseInt(rank, 10);
  const face = ['J', 'Q', 'K'].includes(rank);
  const pips = PIPS[pipCount] || [];

  const classes = [
    'playing-card',
    'card-face',
    suitInfo.red ? 'red' : 'black',
    isDisabled || !isPlayable ? 'disabled' : 'playable',
    isSelected ? 'selected' : '',
    isDragging ? 'dragging-source' : '',
    className,
  ].filter(Boolean).join(' ');

  return (
    <button
      type="button"
      className={classes}
      style={style}
      onClick={onClick}
      onPointerDown={isPlayable && !isDisabled ? onPointerDown : undefined}
      onKeyDown={onKeyDown}
      disabled={isDisabled || !isPlayable}
      data-card-code={code}
      aria-label={rank + ' ' + suitInfo.name}
      aria-pressed={isSelected}
      tabIndex={isPlayable && !isDisabled ? 0 : -1}
    >
      <span className="card-border-glow" aria-hidden="true" />
      <span className="card-corner card-corner-top" aria-hidden="true">
        <b>{rank}</b><i>{suitInfo.symbol}</i>
      </span>
      <span className={'card-art ' + (face ? 'court-face' : '')} aria-hidden="true">
        {face ? (
          <CourtArt rank={rank} symbol={suitInfo.symbol} red={suitInfo.red} />
        ) : (
          <svg className={'pip-art ' + (rank === 'A' ? 'ace-art' : '')} viewBox="0 0 100 100">
            {pips.map(([x, y], index) => (
              <text
                key={x + '-' + y + '-' + index}
                x={x}
                y={y}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={rank === 'A' ? 48 : pipCount >= 8 ? 17 : 21}
                fontWeight="700"
                transform={index % 2 ? 'rotate(180 ' + x + ' ' + y + ')' : undefined}
              >
                {suitInfo.symbol}
              </text>
            ))}
            {rank === 'A' && (
              <circle cx="50" cy="50" r="34" fill="none" stroke="currentColor" strokeOpacity=".18" strokeWidth="1.5" />
            )}
          </svg>
        )}
      </span>
      <span className="card-corner card-corner-bottom" aria-hidden="true">
        <b>{rank}</b><i>{suitInfo.symbol}</i>
      </span>
      <span className="card-foil" aria-hidden="true" />
    </button>
  );
};

export const PlayingCard = React.memo(PlayingCardComponent);
