export interface TelegramWebApp {
  ready(): void;
  expand(): void;
  close(): void;
  initData: string;
  initDataUnsafe: {
    user?: {
      id: number;
      first_name: string;
      last_name?: string;
      username?: string;
    };
    start_param?: string;
  };
  BackButton: {
    show(): void;
    hide(): void;
    onClick(cb: () => void): void;
    offClick(cb: () => void): void;
  };
  HapticFeedback: {
    impactOccurred(style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft'): void;
    notificationOccurred(type: 'error' | 'success' | 'warning'): void;
  };
  setHeaderColor(color: string): void;
  setBackgroundColor(color: string): void;
  disableVerticalSwipes?(): void;
  openTelegramLink?(url: string): void;
}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp;
    };
  }
}

export interface TimeControl {
  key: string;
  label: string;
  initial_ms: number;
  increment_ms: number;
}

export const TIME_CONTROLS: TimeControl[] = [
  { key: 'bullet_1_0', label: '۱+۰ بولت', initial_ms: 60000, increment_ms: 0 },
  { key: 'blitz_3_0', label: '۳+۰ بلیتز', initial_ms: 180000, increment_ms: 0 },
  { key: 'blitz_3_2', label: '۳+۲ بلیتز', initial_ms: 180000, increment_ms: 2000 },
  { key: 'blitz_5_0', label: '۵+۰ بلیتز', initial_ms: 300000, increment_ms: 0 },
  { key: 'rapid_10_0', label: '۱۰+۰ رپید', initial_ms: 600000, increment_ms: 0 },
];

export interface PlayerInfo {
  user_id: number;
  name: string;
}

export interface MoveRecord {
  seq: number;
  uci: string;
  san: string;
  player_id?: number;
}

export interface GameState {
  fen: string;
  turn: 'white' | 'black';
  last_move: string | null;
  check: boolean;
  status: string;
  white_ms: number;
  black_ms: number;
  move_history: MoveRecord[];
  white_id?: number;
  black_id?: number;
  white_name?: string;
  black_name?: string;
  player1_name?: string;
  player2_name?: string;
  result?: string | null;
  end_reason?: string | null;
  draw_offered_by?: number | null;
  time_control?: {
    key: string;
    label: string;
  };
}

export interface PlayerStats {
  wins: number;
  losses: number;
  draws: number;
  games_played: number;
}

export type ServerMessage =
  | { type: 'game.state'; game_id: string; seq: number; payload: GameState }
  | {
      type: 'game.start';
      game_id: string;
      seq: number;
      payload: {
        white: PlayerInfo;
        black: PlayerInfo;
        fen: string;
        time_control: TimeControl;
      };
    }
  | {
      type: 'game.finished';
      game_id: string;
      seq: number;
      payload: {
        result: string;
        reason: string;
        winner_id: number | null;
      };
    }
  | { type: 'game.clock'; game_id: string; payload: { white_ms: number; black_ms: number } }
  | { type: 'game.sync'; game_id: string; seq: number; payload: GameState }
  | { type: 'player.connected'; game_id: string; payload: { user_id: number } }
  | { type: 'player.disconnected'; game_id: string; payload: { user_id: number } }
  | { type: 'game.draw.offer'; game_id: string; payload: { from_id: number } }
  | { type: 'game.draw.declined'; game_id: string; payload: { by_id: number } }
  | { type: 'error'; payload: { code: string; message: string } }
  | { type: 'pong' }
  | { type: 'ping' };

export type Screen = 'lobby' | 'waiting' | 'game';
