import { ServerMessage } from '../types';
import { getTicket } from './api';

type MessageHandler = (msg: ServerMessage) => void;
type StatusHandler = (status: 'connected' | 'connecting' | 'disconnected') => void;

class ChessSocketService {
  private ws: WebSocket | null = null;
  private messageHandlers: Set<MessageHandler> = new Set();
  private statusHandlers: Set<StatusHandler> = new Set();
  private pingInterval: number | null = null;
  private reconnectTimeout: number | null = null;
  private retryAttempt = 0;
  private shouldReconnect = true;
  private connectPromise: Promise<void> | null = null;
  private pendingSyncs: Array<Record<string, any>> = [];

  public status: 'connected' | 'connecting' | 'disconnected' = 'disconnected';

  public onMessage(handler: MessageHandler) {
    this.messageHandlers.add(handler);
    return () => this.messageHandlers.delete(handler);
  }

  public onStatus(handler: StatusHandler) {
    this.statusHandlers.add(handler);
    handler(this.status);
    return () => this.statusHandlers.delete(handler);
  }

  private setStatus(newStatus: 'connected' | 'connecting' | 'disconnected') {
    this.status = newStatus;
    this.statusHandlers.forEach((h) => h(newStatus));
  }

  public async connect(): Promise<void> {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    if (this.connectPromise) {
      return this.connectPromise;
    }

    this.shouldReconnect = true;
    this.setStatus('connecting');

    this.connectPromise = (async () => {
      try {
        const { ticket } = await getTicket();
        if (!this.shouldReconnect) return;

        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${proto}//${window.location.host}/ws/chess?ticket=${encodeURIComponent(ticket)}`;
        const socket = new WebSocket(wsUrl);
        this.ws = socket;

        socket.onopen = () => {
          if (this.ws !== socket) return;
          this.setStatus('connected');
          this.retryAttempt = 0;
          this.startHeartbeat();
          this.flushPendingSyncs();
        };

        socket.onmessage = (event) => {
          if (this.ws !== socket) return;
          try {
            const msg = JSON.parse(event.data) as ServerMessage;
            this.messageHandlers.forEach((handler) => handler(msg));
          } catch (e) {
            console.error('Failed to parse WebSocket JSON', e);
          }
        };

        socket.onclose = () => {
          if (this.ws !== socket) return;
          this.ws = null;
          this.cleanup();
          this.setStatus('disconnected');
          if (this.shouldReconnect) {
            this.scheduleReconnect();
          }
        };

        socket.onerror = (err) => {
          if (this.ws === socket) {
            console.warn('WebSocket connection error:', err);
          }
        };
      } catch (e) {
        this.setStatus('disconnected');
        if (this.shouldReconnect) {
          this.scheduleReconnect();
        }
      }
    })();

    try {
      await this.connectPromise;
    } finally {
      this.connectPromise = null;
    }
  }

  public send(msg: Record<string, any>): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(msg));
        return true;
      } catch (e) {
        console.warn('WebSocket send failed:', e);
        return false;
      }
    }

    // Sync is idempotent and safe to defer until the authenticated socket opens.
    // User actions are deliberately not queued across reconnects.
    if (msg.type === 'game.request_sync' && msg.game_id) {
      this.pendingSyncs = this.pendingSyncs.filter((queued) => queued.game_id !== msg.game_id);
      this.pendingSyncs.push(msg);
      if (!this.ws || this.ws.readyState === WebSocket.CLOSED) {
        void this.connect();
      }
      return true;
    }

    console.warn('Cannot send WebSocket message: socket not open');
    return false;
  }

  private flushPendingSyncs(): void {
    const socket = this.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    while (this.pendingSyncs.length > 0) {
      const msg = this.pendingSyncs.shift();
      if (!msg) continue;
      try {
        socket.send(JSON.stringify(msg));
      } catch (e) {
        this.pendingSyncs.unshift(msg);
        console.warn('Failed to flush queued chess sync request:', e);
        break;
      }
    }
  }

  public disconnect(): void {
    this.shouldReconnect = false;
    this.pendingSyncs = [];
    this.cleanup();
    if (this.ws) {
      const socket = this.ws;
      this.ws = null;
      socket.close();
    }
    this.setStatus('disconnected');
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.pingInterval = window.setInterval(() => {
      this.send({ type: 'ping' });
    }, 25000);
  }

  private stopHeartbeat(): void {
    if (this.pingInterval !== null) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimeout !== null) return;
    this.retryAttempt++;
    // Exponential backoff: 1s, 2s, 4s, capped at 16s
    const delay = Math.min(1000 * Math.pow(2, this.retryAttempt - 1), 16000);
    this.reconnectTimeout = window.setTimeout(() => {
      this.reconnectTimeout = null;
      this.connect();
    }, delay);
  }

  private cleanup(): void {
    this.stopHeartbeat();
    if (this.reconnectTimeout !== null) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }
  }
}

export const chessSocket = new ChessSocketService();
