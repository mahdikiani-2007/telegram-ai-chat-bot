import React from 'react';
import { PlayingCard } from './Cards';

interface CeremonyModalProps {
  ceremonyCards: Record<string, string>;
  hakemSeat: number | null;
  players: Array<{ seat: number; name: string }>;
}

export const CeremonyModal: React.FC<CeremonyModalProps> = ({ ceremonyCards, hakemSeat, players }) => (
  <div className="game-modal-backdrop">
    <section className="game-modal ceremony-modal" role="dialog" aria-modal="true" aria-labelledby="ceremony-title">
      <span className="modal-kicker">THE ROYAL DRAW</span>
      <div className="modal-emblem">♛</div>
      <h2 id="ceremony-title">مراسم تعیین حاکم</h2>
      <p className="modal-description">هر بازیکن یک کارت می‌کشد؛ بالاترین کارت، حاکم دست خواهد بود.</p>

      <div className="ceremony-card-grid">
        {players.map((player) => {
          const card = ceremonyCards[String(player.seat)] || ceremonyCards[player.seat];
          const winner = hakemSeat === player.seat;
          return (
            <div className={'ceremony-player ' + (winner ? 'winner' : '')} key={player.seat}>
              <span className="ceremony-player-name">{player.name}</span>
              {card ? <PlayingCard code={card} isPlayable={false} /> : <PlayingCard code="BACK" />}
              {winner && <span className="ceremony-crown">♛ حاکم</span>}
            </div>
          );
        })}
      </div>

      {hakemSeat !== null && (
        <div className="ceremony-result">
          <span>👑</span>
          <b>{players.find((player) => player.seat === hakemSeat)?.name || 'بازیکن'}</b>
          <span>حاکم شد</span>
        </div>
      )}
    </section>
  </div>
);

interface TrumpSelectModalProps {
  onSelectSuit: (suit: string) => void;
}

const TRUMP_OPTIONS = [
  { key: 'S', name: 'پیک', symbol: '♠', red: false, description: 'قدرت و تمرکز' },
  { key: 'H', name: 'دل', symbol: '♥', red: true, description: 'ریسک حساب‌شده' },
  { key: 'D', name: 'خشت', symbol: '♦', red: true, description: 'بازی تهاجمی' },
  { key: 'C', name: 'گشنیز', symbol: '♣', red: false, description: 'انتخاب هوشمند' },
];

export const TrumpSelectModal: React.FC<TrumpSelectModalProps> = ({ onSelectSuit }) => (
  <div className="game-modal-backdrop">
    <section className="game-modal trump-modal" role="dialog" aria-modal="true" aria-labelledby="trump-title">
      <span className="modal-kicker">YOU ARE THE HAKEM</span>
      <div className="modal-emblem">♛</div>
      <h2 id="trump-title">خال حکم را انتخاب کن</h2>
      <p className="modal-description">پنج کارت اولت را بررسی کن و خالی را انتخاب کن که بیشترین قدرت را به دستت می‌دهد.</p>
      <div className="trump-choice-grid">
        {TRUMP_OPTIONS.map((suit) => (
          <button
            type="button"
            key={suit.key}
            className={'trump-choice ' + (suit.red ? 'red-suit' : 'black-suit')}
            onClick={() => onSelectSuit(suit.key)}
          >
            <span className="trump-choice-symbol">{suit.symbol}</span>
            <b>{suit.name}</b>
            <small>{suit.description}</small>
          </button>
        ))}
      </div>
    </section>
  </div>
);

interface TwoPlayerDraftModalProps {
  offeredCards: string[];
  onSelectCard: (card: string) => void;
}

export const TwoPlayerDraftModal: React.FC<TwoPlayerDraftModalProps> = ({ offeredCards, onSelectCard }) => (
  <div className="game-modal-backdrop">
    <section className="game-modal draft-modal" role="dialog" aria-modal="true" aria-labelledby="draft-title">
      <span className="modal-kicker">TWO PLAYER MODE</span>
      <div className="modal-emblem">♠</div>
      <h2 id="draft-title">کارتت را انتخاب کن</h2>
      <p className="modal-description">از بین این دو کارت، یکی را در دستت نگه دار. کارت دیگر از بازی خارج می‌شود.</p>
      <div className="draft-card-options">
        {offeredCards.map((card) => (
          <button type="button" className="draft-card-option" key={card} onClick={() => onSelectCard(card)}>
            <PlayingCard code={card} isPlayable={true} />
            <span>انتخاب کارت</span>
          </button>
        ))}
      </div>
    </section>
  </div>
);

interface HandSummaryModalProps {
  summary: any;
  gameMode: string;
  players: any[];
}

export const HandSummaryModal: React.FC<HandSummaryModalProps> = ({ summary, gameMode, players }) => {
  if (!summary) return null;

  const isTeam = gameMode === 'hokm_4p';
  const isDraw = Boolean(summary.is_draw);
  const winner = isTeam
    ? (summary.winner_team === null || summary.winner_team === undefined
        ? 'بدون برنده'
        : 'تیم ' + (summary.winner_team === 0 ? 'الف' : 'ب'))
    : (summary.winner_seat === null || summary.winner_seat === undefined
        ? 'بدون برنده'
        : players.find((player) => player.seat === summary.winner_seat)?.name || 'بازیکن برنده');

  const kotLabel = summary.is_hakem_kot ? 'حاکم‌کوت' : summary.is_kot ? 'کوت' : null;
  const trickRows = isTeam
    ? [
        { label: 'تیم الف', value: summary.tricks_team_a ?? 0 },
        { label: 'تیم ب', value: summary.tricks_team_b ?? 0 },
      ]
    : players.map((player) => ({
        label: player.name,
        value: summary.tricks_by_seat?.[String(player.seat)] ?? summary.tricks_by_seat?.[player.seat] ?? 0,
      }));

  return (
    <div className="game-modal-backdrop">
      <section className="game-modal summary-modal" role="dialog" aria-modal="true" aria-labelledby="summary-title">
        <span className="modal-kicker">HAND REPORT</span>
        <div className="modal-emblem">{isDraw ? '🤝' : kotLabel ? '♛' : '♠'}</div>
        <span className="summary-hand-label">نتیجه‌ی دست {summary.hand_number}</span>
        <h2 id="summary-title">{isDraw ? 'دست مساوی شد' : 'برنده: ' + winner}</h2>
        {kotLabel && <div className="summary-kot-ribbon">{kotLabel}</div>}
        <div className="summary-points">
          <b>{summary.points_awarded ?? 0}</b>
          <span>امتیاز کسب‌شده</span>
        </div>
        <div className="summary-tricks">
          {trickRows.map((row) => (
            <div className="summary-trick-row" key={row.label}>
              <span>{row.label}</span>
              <b>{row.value} دور</b>
            </div>
          ))}
        </div>
        <p className="modal-footnote">دست بعدی به‌زودی آغاز می‌شود…</p>
      </section>
    </div>
  );
};

interface MatchOverModalProps {
  isWinner: boolean;
  scoreText: string;
  rematchVotes?: number;
  rematchRequired?: number;
  onRematch: () => void;
  onReturnLobby: () => void;
}

export const MatchOverModal: React.FC<MatchOverModalProps> = ({
  isWinner,
  scoreText,
  rematchVotes = 0,
  rematchRequired = 0,
  onRematch,
  onReturnLobby,
}) => (
  <div className="game-modal-backdrop">
    <section className="game-modal match-modal" role="dialog" aria-modal="true" aria-labelledby="match-title">
      <span className="modal-kicker">MATCH COMPLETE</span>
      <div className={'modal-emblem match-emblem ' + (isWinner ? 'victory' : 'defeat')}>{isWinner ? '🏆' : '♧'}</div>
      <h2 id="match-title">{isWinner ? 'پیروزی باشکوه!' : 'مسابقه به پایان رسید'}</h2>
      <p className="modal-description">نتیجه‌ی نهایی مسابقه</p>
      <div className="match-final-score" dir="ltr">{scoreText}</div>
      <div className="match-actions">
        <button type="button" className="button-primary" onClick={onRematch}>درخواست بازی مجدد</button>
        <button type="button" className="button-secondary" onClick={onReturnLobby}>بازگشت به لابی</button>
      </div>
      {rematchRequired > 1 && (
        <div className="rematch-progress">
          <div className="rematch-progress-label">
            <span>موافقت برای بازی مجدد</span>
            <b>{rematchVotes} از {rematchRequired}</b>
          </div>
          <div className="rematch-progress-track">
            <i style={{ width: Math.min(100, (rematchVotes / rematchRequired) * 100) + '%' }} />
          </div>
        </div>
      )}
    </section>
  </div>
);
