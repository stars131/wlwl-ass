/**
 * Conversation state store — Zustand. Mirrors the server-side state machine.
 * The `state` field is the authoritative server state; `connection` tracks
 * WebSocket lifecycle independently.
 */
import { create } from 'zustand';

export type ServerState =
  | 'IDLE'
  | 'ARMED'
  | 'LISTENING'
  | 'PROCESSING'
  | 'RESPONDING'
  | 'PAUSED';

export type ConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'fatal';

export type Role = 'user' | 'agent' | 'system';

export interface Turn {
  turnId: string;
  role: Role;
  text: string;
  partial: boolean;
  intent?: string;
  ts: number;
}

interface State {
  serverState: ServerState;
  connection: ConnectionState;
  sessionId: string | null;
  turns: Turn[];
  partial: { text: string; conf: number } | null;
  lastError: { code: string; message: string; fatal: boolean } | null;
  micGranted: boolean;
  micDevice: string | null;
  paused: boolean;
  diagnosticsVisible: boolean;
  serverUrl: string;
  authToken: string;

  setServerState: (s: ServerState) => void;
  setConnection: (c: ConnectionState) => void;
  setSession: (id: string | null) => void;
  setMicGranted: (g: boolean) => void;
  setMicDevice: (id: string | null) => void;
  setPaused: (p: boolean) => void;
  toggleDiagnostics: () => void;
  setServerUrl: (u: string) => void;
  setAuthToken: (t: string) => void;
  setError: (e: State['lastError']) => void;
  clearError: () => void;
  setPartial: (text: string, conf: number) => void;
  clearPartial: () => void;
  appendFinal: (turn: Omit<Turn, 'ts'>) => void;
  attachIntent: (turnId: string, intent: string) => void;
  reset: () => void;
}

const DEFAULT_URL =
  (typeof window !== 'undefined' && (window as unknown as { __WLWL_VOICE_WS__?: string })
    .__WLWL_VOICE_WS__) ||
  localStorage.getItem('wlwl.voice.serverUrl') ||
  'ws://127.0.0.1:9700/api/voice/session';

const DEFAULT_TOKEN = localStorage.getItem('wlwl.voice.authToken') || '';

export const useConversation = create<State>((set) => ({
  serverState: 'IDLE',
  connection: 'disconnected',
  sessionId: null,
  turns: [],
  partial: null,
  lastError: null,
  micGranted: false,
  micDevice: null,
  paused: false,
  diagnosticsVisible: false,
  serverUrl: DEFAULT_URL,
  authToken: DEFAULT_TOKEN,

  setServerState: (s) => set({ serverState: s }),
  setConnection: (c) => set({ connection: c }),
  setSession: (id) => set({ sessionId: id }),
  setMicGranted: (g) => set({ micGranted: g }),
  setMicDevice: (id) => {
    if (id) localStorage.setItem('wlwl.voice.micDevice', id);
    set({ micDevice: id });
  },
  setPaused: (p) => set({ paused: p }),
  toggleDiagnostics: () => set((s) => ({ diagnosticsVisible: !s.diagnosticsVisible })),
  setServerUrl: (u) => {
    localStorage.setItem('wlwl.voice.serverUrl', u);
    set({ serverUrl: u });
  },
  setAuthToken: (t) => {
    if (t) localStorage.setItem('wlwl.voice.authToken', t);
    else localStorage.removeItem('wlwl.voice.authToken');
    set({ authToken: t });
  },
  setError: (e) => set({ lastError: e }),
  clearError: () => set({ lastError: null }),
  setPartial: (text, conf) => set({ partial: { text, conf } }),
  clearPartial: () => set({ partial: null }),
  appendFinal: (turn) =>
    set((s) => ({
      turns: [...s.turns, { ...turn, ts: Date.now() }].slice(-200),
      partial: null,
    })),
  attachIntent: (turnId, intent) =>
    set((s) => ({
      turns: s.turns.map((t) => (t.turnId === turnId ? { ...t, intent } : t)),
    })),
  reset: () =>
    set({
      serverState: 'IDLE',
      connection: 'disconnected',
      sessionId: null,
      turns: [],
      partial: null,
      lastError: null,
    }),
}));
