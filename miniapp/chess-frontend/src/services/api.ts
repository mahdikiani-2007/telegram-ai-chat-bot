import { getInitData } from './telegram';
import { PlayerStats, TimeControl } from '../types';

export class ApiError extends Error {
  public readonly code: string;
  public readonly roomId: string | null;
  public readonly status: number;

  constructor(message: string, code: string, roomId: string | null, status: number) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.roomId = roomId;
    this.status = status;
  }
}

async function postJson<T>(url: string, payload: Record<string, any> = {}): Promise<T> {
  const initData = getInitData();
  const urlParams = new URLSearchParams(window.location.search);
  const devUser = urlParams.get('user') || urlParams.get('dev_user') || '1';

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      initData,
      dev_user: initData ? undefined : parseInt(devUser, 10),
      ...payload,
    }),
  });

  const data = await res.json();
  if (!res.ok) {
    throw new ApiError(
      data.message || data.error || `HTTP error ${res.status}`,
      String(data.error || 'request_failed'),
      data.room_id ? String(data.room_id) : null,
      res.status
    );
  }
  return data as T;
}

export async function getTicket(): Promise<{ ticket: string; user_id: number; name: string }> {
  return postJson('/api/chess/ticket');
}

export async function createRoom(timeControlKey: string): Promise<{
  room_id: string;
  invite_code: string;
  time_control: TimeControl;
}> {
  return postJson('/api/chess/create', { time_control: timeControlKey });
}

export async function createBotGame(
  difficulty: 'easy' | 'medium' | 'hard',
  timeControlKey: string
): Promise<{ room_id: string; is_bot_game: true; bot_difficulty: string }> {
  return postJson('/api/chess/bot', {
    difficulty,
    time_control: timeControlKey,
  });
}

export async function cancelRoom(roomId: string): Promise<{ ok: boolean; room_id: string }> {
  return postJson('/api/chess/cancel', { room_id: roomId });
}

export async function joinRoom(
  roomId?: string,
  inviteCode?: string
): Promise<{ room_id: string }> {
  return postJson('/api/chess/join', { room_id: roomId, invite_code: inviteCode });
}

export async function matchmake(timeControlKey: string): Promise<{
  status: 'matched' | 'queued';
  room_id?: string;
}> {
  return postJson('/api/chess/matchmake', { time_control: timeControlKey });
}

export async function cancelMatchmaking(): Promise<{ ok: boolean }> {
  return postJson('/api/chess/matchmake/cancel');
}

export async function getProfile(): Promise<{
  user_id: number;
  name: string;
  stats: PlayerStats;
  active_room: string | null;
}> {
  return postJson('/api/chess/profile');
}

export async function getRoomInfo(roomId: string): Promise<any> {
  const res = await fetch(`/api/chess/room/${roomId}`);
  return res.json();
}
