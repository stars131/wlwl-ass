/**
 * VoiceController — orchestrates capture + socket + playback together.
 * Components subscribe to the conversation store; this module is the only
 * place all three pieces are wired.
 */
import { AudioCapture } from './audio/capture';
import { AudioPlayback } from './audio/playback';
import { VoiceSocket, type ServerFrame } from './ws';
import { useConversation, type ServerState } from '../state/conversation';

export class VoiceController {
  private capture: AudioCapture | null = null;
  private playback: AudioPlayback;
  private socket: VoiceSocket | null = null;

  constructor() {
    this.playback = new AudioPlayback({ mock: true });
  }

  async startMic(deviceId?: string | null): Promise<void> {
    const store = useConversation.getState();
    this.capture?.stop();
    this.capture = new AudioCapture({
      deviceId: deviceId ?? null,
      chunkMs: 250,
      onChunk: (blob) => {
        const s = useConversation.getState();
        if (s.serverState === 'PAUSED' || s.paused) return;
        if (s.serverState === 'IDLE' || s.serverState === 'LISTENING') {
          this.socket?.sendAudio(blob);
        }
      },
    });
    try {
      await this.capture.start();
      store.setMicGranted(true);
      if (deviceId) store.setMicDevice(deviceId);
    } catch (err) {
      store.setMicGranted(false);
      store.setError({
        code: 'mic_denied',
        message: err instanceof Error ? err.message : 'unknown error',
        fatal: true,
      });
      throw err;
    }
  }

  stopMic(): void {
    this.capture?.stop();
    this.capture = null;
  }

  setMuted(muted: boolean): void {
    useConversation.getState().setPaused(muted);
    this.capture?.setMuted(muted);
  }

  connect(): void {
    if (this.socket) return;
    const store = useConversation.getState();
    this.socket = new VoiceSocket(store.serverUrl, store.authToken, {
      onConnecting: () => useConversation.getState().setConnection('connecting'),
      onOpen: () => useConversation.getState().setConnection('connected'),
      onClose: (clean) => {
        const cs = clean ? 'disconnected' : 'reconnecting';
        useConversation.getState().setConnection(cs);
      },
      onError: (e) =>
        useConversation.getState().setError({ ...e, fatal: true }),
      onText: (frame) => this.dispatchText(frame),
      onBinary: (buf) => this.playback.feed(buf),
    });
    this.socket.open();
  }

  disconnect(): void {
    this.socket?.close();
    this.socket = null;
    useConversation.getState().setConnection('disconnected');
  }

  goodbye(): void {
    this.socket?.sendControl({ type: 'goodbye' });
  }

  pauseUser(): void {
    this.socket?.sendControl({ type: 'pause' });
    this.setMuted(true);
  }

  resumeUser(): void {
    this.socket?.sendControl({ type: 'resume' });
    this.setMuted(false);
  }

  forgetSession(): void {
    this.socket?.sendControl({ type: 'forget_session' });
  }

  async unlockAudio(): Promise<void> {
    await this.playback.unlock();
  }

  private dispatchText(frame: ServerFrame): void {
    const s = useConversation.getState();
    switch (frame.type) {
      case 'ready':
        s.setSession(frame.session_id);
        break;
      case 'state': {
        const v = frame.value as ServerState;
        if (v === 'PAUSED') s.setPaused(true);
        else {
          s.setPaused(false);
          s.setServerState(v);
        }
        break;
      }
      case 'transcript.partial':
        s.setPartial(frame.text, frame.conf);
        break;
      case 'transcript.final':
        s.appendFinal({
          turnId: frame.turn_id,
          role: frame.role,
          text: frame.text,
          partial: false,
        });
        break;
      case 'intent':
        // attach intent label to the most recent user turn
        {
          const lastUser = [...s.turns].reverse().find((t) => t.role === 'user');
          if (lastUser) s.attachIntent(lastUser.turnId, frame.intent);
        }
        break;
      case 'tts.start':
        this.playback.startTurn(frame.turn_id);
        break;
      case 'tts.end':
        this.playback.endTurn();
        break;
      case 'error':
        s.setError({ code: frame.code, message: frame.message, fatal: frame.fatal });
        break;
    }
  }
}

let _controllerSingleton: VoiceController | null = null;
export function getController(): VoiceController {
  if (!_controllerSingleton) _controllerSingleton = new VoiceController();
  return _controllerSingleton;
}
