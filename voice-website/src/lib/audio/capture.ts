/**
 * AudioCapture — captures the user's microphone, encodes to a Blob via
 * MediaRecorder, and emits chunks via callback.
 *
 * Trade-off: MediaRecorder produces a webm/ogg-OPUS *container* that
 * accumulates over the recorder's lifetime. For low-latency streaming we
 * frequently start/stop a fresh recorder per chunk window (~ 250 ms) so
 * each emitted Blob is decode-able standalone. This is acceptable for the
 * MVP transport — the server treats each binary frame as a fresh audio
 * snippet. A future revision can adopt AudioWorklet + a JS Opus encoder
 * for true 20 ms frames.
 */

export type AudioCaptureCallback = (blob: Blob) => void;

interface CaptureOptions {
  onChunk: AudioCaptureCallback;
  chunkMs?: number;
  deviceId?: string | null;
}

export class AudioCapture {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private chunkMs: number;
  private deviceId: string | null;
  private onChunk: AudioCaptureCallback;
  private restartTimer: number | null = null;
  private running = false;
  private muted = false;

  static async getInputDevices(): Promise<MediaDeviceInfo[]> {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === 'audioinput');
  }

  constructor(opts: CaptureOptions) {
    this.onChunk = opts.onChunk;
    this.chunkMs = opts.chunkMs ?? 250;
    this.deviceId = opts.deviceId ?? null;
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: this.deviceId
        ? { deviceId: { exact: this.deviceId }, channelCount: 1, sampleRate: 16000 }
        : { channelCount: 1, sampleRate: 16000 },
      video: false,
    });
    this.running = true;
    this.spinUpRecorder();
  }

  stop(): void {
    this.running = false;
    if (this.restartTimer) {
      window.clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    try {
      this.recorder?.stop();
    } catch {
      /* ignore */
    }
    this.recorder = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.stream?.getAudioTracks().forEach((t) => {
      t.enabled = !muted;
    });
  }

  /** Returns the live AudioContext-bound source for VU-meter / waveform UIs. */
  getAnalyser(): AnalyserNode | null {
    if (!this.stream) return null;
    try {
      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(this.stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      return analyser;
    } catch {
      return null;
    }
  }

  private spinUpRecorder(): void {
    if (!this.stream || !this.running) return;
    const mime = this.pickMime();
    const rec = new MediaRecorder(this.stream, mime ? { mimeType: mime, audioBitsPerSecond: 24000 } : undefined);
    this.recorder = rec;

    rec.ondataavailable = (ev) => {
      if (this.muted || !this.running) return;
      if (ev.data && ev.data.size > 0) this.onChunk(ev.data);
    };
    rec.onstop = () => {
      if (!this.running) return;
      // Cycle: spin a fresh recorder so each blob is independently decodable.
      this.restartTimer = window.setTimeout(() => this.spinUpRecorder(), 0);
    };
    rec.start();
    // Stop after chunkMs to flush a complete blob; onstop spins a new one.
    window.setTimeout(() => {
      try {
        if (rec.state !== 'inactive') rec.stop();
      } catch {
        /* ignore */
      }
    }, this.chunkMs);
  }

  private pickMime(): string | null {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/ogg;codecs=opus',
      'audio/webm',
      'audio/mp4',
    ];
    for (const c of candidates) {
      if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(c)) return c;
    }
    return null;
  }
}
