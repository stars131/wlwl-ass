/**
 * VoiceSocket — typed WebSocket client speaking the wlwl-ass voice protocol.
 * Implements PRD §6 frames, auto-reconnect with exponential backoff, and
 * pluggable handlers for state / transcript / TTS events.
 */

export type ServerFrame =
  | { type: 'ready'; session_id: string; tts_format: { codec: string; sample_rate: number; channels: number } }
  | { type: 'state'; value: string; note?: string }
  | { type: 'transcript.partial'; text: string; conf: number }
  | { type: 'transcript.final'; text: string; conf: number; role: 'user' | 'agent'; turn_id: string }
  | { type: 'intent'; intent: string; confidence: number; args: Record<string, unknown> }
  | { type: 'dispatch'; worker: string; cap: string; call_id: string }
  | { type: 'dispatch_result'; call_id: string; ok: boolean; summary?: string; error?: { code: string; message: string } }
  | { type: 'tts.start'; turn_id: string }
  | { type: 'tts.end'; turn_id: string }
  | { type: 'error'; code: string; message: string; fatal: boolean };

export interface ClientFrame {
  type: 'hello' | 'pause' | 'resume' | 'forget_session' | 'goodbye';
  client?: string;
  audio_format?: { codec: string; sample_rate: number; channels: number };
}

export interface SocketHandlers {
  onConnecting?: () => void;
  onOpen?: () => void;
  onText?: (frame: ServerFrame) => void;
  onBinary?: (frame: ArrayBuffer) => void;
  onClose?: (clean: boolean) => void;
  onError?: (e: { code: string; message: string }) => void;
}

const BACKOFF_MS = [250, 500, 1000, 2000, 5000, 5000];

export class VoiceSocket {
  private url: string;
  private authToken: string;
  private ws: WebSocket | null = null;
  private handlers: SocketHandlers;
  private wantOpen = false;
  private reconnectIdx = 0;
  private reconnectTimer: number | null = null;
  private resumeSession: string | null = null;
  private outboundAudio: Blob[] = [];

  constructor(url: string, authToken: string, handlers: SocketHandlers) {
    this.url = url;
    this.authToken = authToken;
    this.handlers = handlers;
  }

  open(): void {
    this.wantOpen = true;
    this.connect();
  }

  close(reason = 'client_close'): void {
    this.wantOpen = false;
    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.send(JSON.stringify({ type: 'goodbye' } as ClientFrame));
      } catch {
        /* ignore */
      }
      try {
        this.ws.close(1000, reason);
      } catch {
        /* ignore */
      }
    }
    this.ws = null;
  }

  sendControl(frame: ClientFrame): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }

  sendAudio(blob: Blob): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(blob);
    } else {
      // Buffer ≤ 5s; drop oldest if over.
      this.outboundAudio.push(blob);
      while (this.outboundAudio.length > 200) this.outboundAudio.shift();
    }
  }

  private connect(): void {
    this.handlers.onConnecting?.();
    const url = this.buildUrl();
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      this.scheduleReconnect();
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectIdx = 0;
      this.handlers.onOpen?.();
      ws.send(
        JSON.stringify({
          type: 'hello',
          client: 'web@1.0',
          audio_format: { codec: 'opus', sample_rate: 16000, channels: 1 },
        } as ClientFrame),
      );
      // flush buffered audio
      while (this.outboundAudio.length > 0) {
        const blob = this.outboundAudio.shift()!;
        ws.send(blob);
      }
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try {
          const frame: ServerFrame = JSON.parse(ev.data);
          if (frame.type === 'ready') this.resumeSession = frame.session_id;
          if (frame.type === 'error' && frame.fatal) {
            this.wantOpen = false;
            this.handlers.onError?.({ code: frame.code, message: frame.message });
          }
          this.handlers.onText?.(frame);
        } catch {
          /* malformed text frame */
        }
      } else if (ev.data instanceof ArrayBuffer) {
        this.handlers.onBinary?.(ev.data);
      } else if (ev.data instanceof Blob) {
        ev.data.arrayBuffer().then((b) => this.handlers.onBinary?.(b));
      }
    };

    ws.onerror = () => {
      // ws.onclose follows; handle reconnection there
    };

    ws.onclose = (ev) => {
      this.handlers.onClose?.(ev.wasClean);
      this.ws = null;
      if (this.wantOpen) this.scheduleReconnect();
    };
  }

  private buildUrl(): string {
    let url = this.url;
    const params: string[] = [];
    if (this.resumeSession) {
      params.push(`session=${encodeURIComponent(this.resumeSession)}`);
    }
    if (this.authToken) {
      // Browsers cannot set custom WebSocket headers, so tunnel the bearer
      // token through the connection URL for deployments that enable auth.
      params.push(`token=${encodeURIComponent(this.authToken)}`);
    }
    if (params.length > 0) {
      const sep = url.includes('?') ? '&' : '?';
      url += `${sep}${params.join('&')}`;
    }
    return url;
  }

  private scheduleReconnect(): void {
    if (!this.wantOpen) return;
    const delay = BACKOFF_MS[Math.min(this.reconnectIdx, BACKOFF_MS.length - 1)];
    this.reconnectIdx++;
    this.reconnectTimer = window.setTimeout(() => this.connect(), delay);
  }
}
