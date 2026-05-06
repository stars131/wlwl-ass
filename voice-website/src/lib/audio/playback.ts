/**
 * AudioPlayback — receives binary frames (4-byte LE seq + payload bytes),
 * decodes via AudioContext.decodeAudioData when payload looks like an
 * audio container, and falls back to a no-op for the mock TTS format used
 * by the wlwl-ass MVP (which sends base64-decoded UTF-8 text bytes).
 *
 * Live MiniMax T2A returns Opus chunks the browser can decode directly —
 * we hand each blob to AudioContext.decodeAudioData, schedule sequentially,
 * and the queue drains seamlessly.
 */

export class AudioPlayback {
  private ctx: AudioContext;
  private nextStart = 0;
  private chunks: Map<string, Map<number, Uint8Array>> = new Map();
  private currentTurn: string | null = null;
  private active = false;
  private mock: boolean;

  constructor({ mock = false }: { mock?: boolean } = {}) {
    this.ctx = new AudioContext();
    this.mock = mock;
  }

  async unlock(): Promise<void> {
    // Browsers (especially Safari) require a user gesture to start audio.
    if (this.ctx.state === 'suspended') {
      try {
        await this.ctx.resume();
      } catch {
        /* ignore */
      }
    }
  }

  startTurn(turnId: string): void {
    this.currentTurn = turnId;
    this.chunks.set(turnId, new Map());
    this.nextStart = this.ctx.currentTime;
    this.active = true;
  }

  /** Feed one binary frame from the WebSocket. */
  async feed(frame: ArrayBuffer): Promise<void> {
    if (!this.currentTurn || frame.byteLength < 4) return;
    const view = new DataView(frame);
    const seq = view.getUint32(0, true);
    const payload = new Uint8Array(frame, 4);
    const map = this.chunks.get(this.currentTurn);
    if (!map) return;
    map.set(seq, payload);
    if (this.mock) {
      // Mock TTS: payload is text bytes, not playable audio. We still log
      // chunk arrival so UI can show progress.
      return;
    }
    try {
      const buffer = await this.ctx.decodeAudioData(payload.slice().buffer);
      this.schedule(buffer);
    } catch {
      /* not a decodable chunk; the mock case may land here too */
    }
  }

  endTurn(): void {
    this.active = false;
    this.currentTurn = null;
  }

  /** Reconstruct full audio from stored seq map (for "listen-back" UX, future). */
  assembleTurn(turnId: string): Uint8Array {
    const map = this.chunks.get(turnId);
    if (!map) return new Uint8Array();
    const seqs = [...map.keys()].sort((a, b) => a - b);
    const total = seqs.reduce((acc, s) => acc + (map.get(s)?.length ?? 0), 0);
    const out = new Uint8Array(total);
    let off = 0;
    for (const s of seqs) {
      const part = map.get(s);
      if (!part) continue;
      out.set(part, off);
      off += part.length;
    }
    return out;
  }

  setVolume(_v: number): void {
    // GainNode wiring is straightforward; deferring to keep MVP code small.
  }

  isActive(): boolean {
    return this.active;
  }

  private schedule(buffer: AudioBuffer): void {
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const start = Math.max(this.ctx.currentTime + 0.02, this.nextStart);
    src.start(start);
    this.nextStart = start + buffer.duration;
  }
}
