import React from 'react';
import { ChessClock } from './ChessClock';
import { ChessPiece } from './ChessPieces';

interface PlayerCardProps {
  name: string;
  color: 'white' | 'black';
  remainingMs: number;
  isActiveTurn: boolean;
  isConnected: boolean;
  rating?: number;
}

export const PlayerCard: React.FC<PlayerCardProps> = ({
  name,
  color,
  remainingMs,
  isActiveTurn,
  isConnected,
}) => {
  const initialLetter = name.trim().charAt(0) || 'U';

  return (
    <div className={`player-bar ${isActiveTurn ? 'active-turn' : ''}`}>
      <div className="player-info">
        <div className="player-avatar">{initialLetter}</div>
        <div className="player-meta">
          <div className="player-name">
            {name}
            <span style={{ display: 'inline-flex', width: 18, height: 18 }}>
              <ChessPiece type="p" color={color === 'white' ? 'w' : 'b'} />
            </span>
          </div>
          <div className="player-sub">
            <span className={isConnected ? 'dot-online' : 'dot-offline'} />
            <span>{isConnected ? 'متصل' : 'قطع ارتباط'}</span>
          </div>
        </div>
      </div>

      <ChessClock remainingMs={remainingMs} isActive={isActiveTurn} />
    </div>
  );
};
