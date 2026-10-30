import React from 'react';
import { ChessPiece } from './ChessPieces';

// -------------------------------------------------------------
// Promotion Modal
// -------------------------------------------------------------
interface PromotionModalProps {
  color: 'white' | 'black';
  onSelect: (piece: 'q' | 'r' | 'b' | 'n') => void;
  onCancel: () => void;
}

export const PromotionModal: React.FC<PromotionModalProps> = ({ color, onSelect, onCancel }) => {
  const pieceColor = color === 'white' ? 'w' : 'b';
  const choices: ('q' | 'r' | 'b' | 'n')[] = ['q', 'r', 'b', 'n'];

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <h3 style={{ fontSize: 18, fontWeight: 900 }}>ارتقای پیاده</h3>
        <p style={{ fontSize: 13, color: 'var(--dim)', marginTop: 4 }}>مهره مورد نظرتان را برای ارتقا انتخاب کنید:</p>

        <div className="promotion-pieces">
          {choices.map((choice) => (
            <div key={choice} className="promotion-choice" onClick={() => onSelect(choice)}>
              <div style={{ width: 44, height: 44 }}>
                <ChessPiece type={choice} color={pieceColor} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

// -------------------------------------------------------------
// Resign Confirm Modal
// -------------------------------------------------------------
interface ResignConfirmModalProps {
  isOpen: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export const ResignConfirmModal: React.FC<ResignConfirmModalProps> = ({
  isOpen,
  onConfirm,
  onCancel,
}) => {
  if (!isOpen) return null;

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontSize: 42, marginBottom: 8 }}>🏳️</div>
        <h3 style={{ fontSize: 19, fontWeight: 900 }}>آیا از تسلیم شدن مطمئنید؟</h3>
        <p style={{ fontSize: 13, color: 'var(--dim)', margin: '8px 0 18px', lineHeight: 1.8 }}>
          با تسلیم شدن، امتیاز بازی به حریف شما واگذار خواهد شد.
        </p>

        <div style={{ display: 'flex', gap: 10 }}>
          <button className="btn-secondary" onClick={onCancel}>
            انصراف
          </button>
          <button
            className="btn-primary"
            style={{ background: 'var(--red)', color: '#fff', boxShadow: 'none' }}
            onClick={onConfirm}
          >
            بله، تسلیم
          </button>
        </div>
      </div>
    </div>
  );
};

// -------------------------------------------------------------
// Game Result Modal
// -------------------------------------------------------------
interface ResultModalProps {
  isOpen: boolean;
  result: string | null;
  reason: string | null;
  myColor: 'white' | 'black' | null;
  onLobbyClick: () => void;
}

export const ResultModal: React.FC<ResultModalProps> = ({
  isOpen,
  result,
  reason,
  myColor,
  onLobbyClick,
}) => {
  if (!isOpen || !result) return null;

  const isDraw = result === 'draw';
  const isWhiteWin = result === 'white_wins';
  const isBlackWin = result === 'black_wins';

  let title = 'پایان بازی';
  let badgeColor = 'var(--cyan)';
  let emoji = '⚔️';

  if (isDraw) {
    title = 'تساوی';
    badgeColor = 'var(--amber)';
    emoji = '🤝';
  } else if ((isWhiteWin && myColor === 'white') || (isBlackWin && myColor === 'black')) {
    title = 'پیروزی شما! 🏆';
    badgeColor = 'var(--lime)';
    emoji = '🎉';
  } else if (myColor !== null) {
    title = 'شکست 💀';
    badgeColor = 'var(--red)';
    emoji = '😔';
  } else {
    title = isWhiteWin ? 'پیروزی سفید' : 'پیروزی سیاه';
  }

  const reasonLabels: Record<string, string> = {
    checkmate: 'کیش و مات',
    resignation: 'تسلیم حریف',
    timeout: 'پایان زمان',
    stalemate: 'پات (بن‌بست)',
    insufficient_material: 'مهره‌های ناکافی برای مات',
    threefold_repetition: 'تکرار سه‌باره حرکت',
    draw_agreement: 'توافق طرفین',
    seventy_five_moves: 'قانون ۷۵ حرکت',
  };

  const reasonText = reasonLabels[reason || ''] || reason || 'پایان بازی';

  return (
    <div className="modal-backdrop">
      <div className="modal-content">
        <div style={{ fontSize: 52, margin: '6px 0' }}>{emoji}</div>
        <h2 style={{ fontSize: 24, fontWeight: 900, color: badgeColor, marginBottom: 6 }}>{title}</h2>
        <div
          style={{
            display: 'inline-block',
            padding: '5px 14px',
            background: 'var(--panel)',
            border: '1px solid var(--panel-line)',
            borderRadius: 999,
            fontSize: 13,
            color: 'var(--dim)',
            marginBottom: 20,
          }}
        >
          دلیل: <b style={{ color: 'var(--text)' }}>{reasonText}</b>
        </div>

        <button className="btn-primary" onClick={onLobbyClick}>
          بازگشت به لابی
        </button>
      </div>
    </div>
  );
};
