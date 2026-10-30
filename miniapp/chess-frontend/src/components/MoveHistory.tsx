import React from 'react';
import { MoveRecord } from '../types';

interface MoveHistoryProps {
  moves: MoveRecord[];
  onClose: () => void;
}

export const MoveHistory: React.FC<MoveHistoryProps> = ({ moves, onClose }) => {
  // Group moves into pairs (White move, Black move)
  const movePairs: { num: number; white: string; black?: string }[] = [];
  for (let i = 0; i < moves.length; i += 2) {
    movePairs.push({
      num: Math.floor(i / 2) + 1,
      white: moves[i]?.san || moves[i]?.uci || '',
      black: moves[i + 1]?.san || moves[i + 1]?.uci,
    });
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ textAlign: 'right' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h3 style={{ fontSize: 18, fontWeight: 900 }}>📜 تاریخچه حرکات</h3>
          <button
            onClick={onClose}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--dim)',
              fontSize: 20,
              cursor: 'pointer',
            }}
          >
            ✕
          </button>
        </div>

        <div
          style={{
            maxHeight: 280,
            overflowY: 'auto',
            paddingRight: 6,
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          {movePairs.length === 0 ? (
            <div style={{ color: 'var(--dim)', textAlign: 'center', padding: 20 }}>هنوز حرکتی انجام نشده است.</div>
          ) : (
            movePairs.map((p) => (
              <div
                key={p.num}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  padding: '6px 12px',
                  background: 'var(--panel)',
                  borderRadius: 8,
                  fontSize: 14,
                  fontFamily: 'monospace',
                }}
              >
                <span style={{ color: 'var(--dim)', width: 32 }}>{p.num}.</span>
                <span style={{ flex: 1, fontWeight: 700, color: 'var(--text)' }}>{p.white}</span>
                <span style={{ flex: 1, fontWeight: 700, color: 'var(--cyan)' }}>{p.black || '...'}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
