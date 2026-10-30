import React from 'react';

interface PieceProps {
  type: string; // 'p' | 'n' | 'b' | 'r' | 'q' | 'k'
  color: 'w' | 'b';
}

export const ChessPiece: React.FC<PieceProps> = ({ type, color }) => {
  const isWhite = color === 'w';
  const fill = isWhite ? '#FFFFFF' : '#1E293B';
  const stroke = isWhite ? '#1E293B' : '#F8FAFC';
  const strokeWidth = '1.5';

  switch (type.toLowerCase()) {
    case 'k':
      return (
        <svg viewBox="0 0 45 45" className="chess-svg">
          <g fill="none" fillRule="evenodd" stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
            <path d="M22.5 11.63V6M20 8h5" stroke={stroke} strokeLinejoin="miter" />
            <path
              d="M22.5 25s4.5-7.5 3-10.5c0 0-1-2.5-3-2.5s-3 2.5-3 2.5c-1.5 3 3 10.5 3 10.5"
              fill={fill}
              stroke={stroke}
            />
            <path
              d="M11.5 37c5.5 3.5 15.5 3.5 21 0v-7s9-4.5 6-10.5c-4-6.5-13.5-3.5-16 4V23c-2.5-7.5-12-10.5-16-4-3 6 6 10.5 6 10.5v7z"
              fill={fill}
              stroke={stroke}
            />
            <path d="M11.5 30c5.5-3 15.5-3 21 0M11.5 33.5c5.5-3 15.5-3 21 0M11.5 37c5.5-3 15.5-3 21 0" stroke={stroke} />
          </g>
        </svg>
      );

    case 'q':
      return (
        <svg viewBox="0 0 45 45" className="chess-svg">
          <g fill="none" fillRule="evenodd" stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
            <path
              d="M9 26c8.5-1.5 21-1.5 27 0l2-12-7 11V11l-5.5 13.5-3-15-3 15-5.5-14V25L7 14l2 12z"
              fill={fill}
              stroke={stroke}
            />
            <path
              d="M9 26c0 2 1.5 2 2.5 4 1 1.5 1 1 .5 3.5-1.5 1-1.5 2.5-1.5 2.5-1.5 1.5.5 2.5.5 2.5 6.5 1 16.5 1 23 0 0 0 2-1 .5-2.5 0 0 0-1.5-1.5-2.5-.5-2.5-.5-2 .5-3.5 1-2 2.5-2 2.5-4-8.5-1.5-18.5-1.5-27 0z"
              fill={fill}
              stroke={stroke}
            />
            <path d="M11.5 30c3.5-1 18.5-1 22 0M12 33.5c6-1 15-1 21 0" stroke={stroke} />
            <circle cx="6" cy="12" r="2" fill={fill} stroke={stroke} />
            <circle cx="14" cy="9" r="2" fill={fill} stroke={stroke} />
            <circle cx="22.5" cy="8" r="2" fill={fill} stroke={stroke} />
            <circle cx="31" cy="9" r="2" fill={fill} stroke={stroke} />
            <circle cx="39" cy="12" r="2" fill={fill} stroke={stroke} />
          </g>
        </svg>
      );

    case 'r':
      return (
        <svg viewBox="0 0 45 45" className="chess-svg">
          <g fill="none" fillRule="evenodd" stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
            <path
              d="M9 39h27v-3H9v3zm3-3v-1.5h21V36H12zm-1-1.5l2-1.5h19l2 1.5H11zm3-1.5V14h17v19H14zm-3-19l1-2h21l1 2H11zm2-2V9h4v3h5V9h6v3h4V9h4v3H13z"
              fill={fill}
              stroke={stroke}
            />
            <path d="M14 29.5h17M14 16.5h17M11 14h23" stroke={stroke} />
          </g>
        </svg>
      );

    case 'b':
      return (
        <svg viewBox="0 0 45 45" className="chess-svg">
          <g fill="none" fillRule="evenodd" stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
            <g fill={fill} stroke={stroke}>
              <path d="M9 36c3.39-.97 10.11.43 13.5-2 3.39 2.43 10.11 1.03 13.5 2 0 0 1.65.54 3 2-.68.97-1.65.99-3 .5-3.39-.97-10.11.46-13.5-1-3.39 1.46-10.11.03-13.5 1-1.35.49-2.32.47-3-.5 1.35-1.94 3-2 3-2z" />
              <path d="M15 32c2.5 2.5 12.5 2.5 15 0 .5-1.5 0-2 0-2 0-2.5-2.5-4-2.5-4 5.5-1.5 6-11.5-5-15.5-11 4-10.5 14-5 15.5 0 0-2.5 1.5-2.5 4 0 0-.5.5 0 2z" />
              <path d="M25 8a2.5 2.5 0 1 1-5 0 2.5 2.5 0 1 1 5 0z" />
            </g>
            <path d="M17.5 26h10M15 30h15" stroke={stroke} />
            <path d="M22.5 15.5v5M20 18h5" stroke={stroke} strokeLinejoin="miter" />
          </g>
        </svg>
      );

    case 'n':
      return (
        <svg viewBox="0 0 45 45" className="chess-svg">
          <g fill="none" fillRule="evenodd" stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
            <path
              d="M22 10c10.5 1 16.5 8 16 29H15c0-9 10-6.5 8-21"
              fill={fill}
              stroke={stroke}
            />
            <path
              d="M24 18c.38 2.91-5.55 7.37-8 9-3 2-2.82 4.34-5 4-1.042-.94 1.41-3.04 0-3-1 0-.632 1.2-1 2.5-.323 1.14-1.41.97-2 1.5-.788.72-1.378 2.01-1 3 0 0 1.5.5 3 0 1.5-.5 3-2 4-2 1.5 0 2.5 1 3 2 1.5 2 4.5 1 6 1s4.5-1 6-3c2-2 2-6.5 1-10.5-1-4-4.5-6.5-6-7z"
              fill={fill}
              stroke={stroke}
            />
            <circle cx="9.5" cy="25.5" r="1" fill={stroke} />
            <circle cx="15" cy="14.5" r="1.5" fill={stroke} />
          </g>
        </svg>
      );

    case 'p':
    default:
      return (
        <svg viewBox="0 0 45 45" className="chess-svg">
          <path
            d="M22.5 9c-2.21 0-4 1.79-4 4 0 .89.29 1.71.78 2.38C17.33 16.5 16 18.59 16 21c0 2.03.94 3.84 2.41 5.03-3 1.06-7.41 5.55-7.41 13.47h23c0-7.92-4.41-12.41-7.41-13.47 1.47-1.19 2.41-3 2.41-5.03 0-2.41-1.33-4.5-3.28-5.62.49-.67.78-1.49.78-2.38 0-2.21-1.79-4-4-4z"
            fill={fill}
            stroke={stroke}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      );
  }
};
