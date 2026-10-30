import React from 'react';
import { PlayingCard } from './Cards';

interface CeremonyModalProps {
  ceremonyCards: Record<string, string>;
  hakemSeat: number | null;
  players: Array<{ seat: number; name: string }>;
}

export const CeremonyModal: React.FC<CeremonyModalProps> = ({
  ceremonyCards,
  hakemSeat,
  players,
}) => {
  return (
    <div className="modal-overlay">
      <div className="modal-card">
        <h2 style={{ color: 'var(--gold)', fontSize: 20, marginBottom: 8 }}>👑 مراسم تعیین حاکم</h2>
        <p style={{ color: 'var(--text-dim)', fontSize: 13, marginBottom: 18 }}>
          هر بازیکن یک کارت می‌کشد. بالاترین کارت (با اولویت پیک &gt; دل &gt; خشت &gt; گشنیز) حاکم می‌شود.
        </p>

        <div style={{ display: 'flex', justifyContent: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
          {players.map((p) => {
            const cardCode = ceremonyCards[String(p.seat)] || ceremonyCards[p.seat];
            const isHakem = hakemSeat === p.seat;
            return (
              <div key={p.seat} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: isHakem ? 'var(--gold)' : 'var(--text-main)' }}>
                  {p.name} {isHakem && '👑'}
                </span>
                {cardCode ? (
                  <PlayingCard code={cardCode} isPlayable={false} />
                ) : (
                  <PlayingCard code="BACK" isPlayable={false} />
                )}
              </div>
            );
          })}
        </div>

        {hakemSeat !== null && (
          <div style={{ color: 'var(--gold-light)', fontWeight: 800, fontSize: 15, marginTop: 10 }}>
            {players.find((p) => p.seat === hakemSeat)?.name} حاکم شد! 🃏
          </div>
        )}
      </div>
    </div>
  );
};

interface TrumpSelectModalProps {
  onSelectSuit: (suit: string) => void;
}

export const TrumpSelectModal: React.FC<TrumpSelectModalProps> = ({ onSelectSuit }) => {
  const suits = [
    { key: 'S', name: 'پیک', symbol: '♠', color: '#0f172a' },
    { key: 'H', name: 'دل', symbol: '♥', color: '#dc2626' },
    { key: 'D', name: 'خشت', symbol: '♦', color: '#dc2626' },
    { key: 'C', name: 'گشنیز', symbol: '♣', color: '#0f172a' },
  ];

  return (
    <div className="modal-overlay">
      <div className="modal-card">
        <h2 style={{ color: 'var(--gold)', fontSize: 22, marginBottom: 6 }}>👑 شما حاکم هستید</h2>
        <p style={{ color: 'var(--text-dim)', fontSize: 14, marginBottom: 20 }}>
          بر اساس ۵ برگ نخست خود، خال حکم را برگزینید:
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 12 }}>
          {suits.map((s) => (
            <button
              key={s.key}
              onClick={() => onSelectSuit(s.key)}
              style={{
                background: 'rgba(255, 255, 255, 0.95)',
                color: s.color,
                border: '2px solid var(--gold)',
                borderRadius: 16,
                padding: '16px 12px',
                fontSize: 20,
                fontWeight: 900,
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 4,
                boxShadow: '0 8px 20px rgba(0,0,0,0.3)',
                transition: 'transform 0.15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.transform = 'scale(1.05)')}
              onMouseLeave={(e) => (e.currentTarget.style.transform = 'scale(1)')}
            >
              <span style={{ fontSize: 32 }}>{s.symbol}</span>
              <span style={{ fontSize: 16, fontFamily: 'Vazirmatn' }}>{s.name}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};

interface TwoPlayerDraftModalProps {
  offeredCards: string[];
  onSelectCard: (card: string) => void;
}

export const TwoPlayerDraftModal: React.FC<TwoPlayerDraftModalProps> = ({
  offeredCards,
  onSelectCard,
}) => {
  return (
    <div className="modal-overlay">
      <div className="modal-card">
        <h2 style={{ color: 'var(--gold)', fontSize: 20, marginBottom: 6 }}>🃏 انتخاب کارت (۲ نفره)</h2>
        <p style={{ color: 'var(--text-dim)', fontSize: 13, marginBottom: 18 }}>
          کارتی که می‌خواهید در دست خود نگه دارید را لمس کنید. کارت دیگر به صورت پنهان می‌سوزد:
        </p>

        <div style={{ display: 'flex', justifyContent: 'center', gap: 24, marginBottom: 10 }}>
          {offeredCards.map((code) => (
            <div
              key={code}
              onClick={() => onSelectCard(code)}
              style={{ cursor: 'pointer', transform: 'scale(1.15)', transition: 'transform 0.15s ease' }}
            >
              <PlayingCard code={code} isPlayable={true} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

interface MatchOverModalProps {
  isWinner: boolean;
  scoreText: string;
  onRematch: () => void;
  onReturnLobby: () => void;
}

export const MatchOverModal: React.FC<MatchOverModalProps> = ({
  isWinner,
  scoreText,
  onRematch,
  onReturnLobby,
}) => {
  return (
    <div className="modal-overlay">
      <div className="modal-card">
        <div style={{ fontSize: 48, marginBottom: 8 }}>{isWinner ? '🏆' : '💔'}</div>
        <h2 style={{ color: isWinner ? 'var(--gold)' : 'var(--danger)', fontSize: 22, marginBottom: 8 }}>
          {isWinner ? 'پیروزی در مسابقه!' : 'شکست در مسابقه'}
        </h2>
        <p style={{ color: 'var(--text-main)', fontSize: 16, fontWeight: 800, marginBottom: 20 }}>
          نتیجه نهایی: {scoreText}
        </p>

        <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
          <button className="gold-btn" onClick={onRematch}>
            🔄 بازی دوباره
          </button>
          <button className="outline-btn" onClick={onReturnLobby}>
            🏠 خروج به لابی
          </button>
        </div>
      </div>
    </div>
  );
};
