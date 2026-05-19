import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './radarApi';

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  (window as unknown as { __GA_API_BASE__?: string }).__GA_API_BASE__ = 'http://t.local';
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('radarApi', () => {
  it('getRadarStatus parses payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        alive: true,
        pid: 28296,
        log_path: 'temp/logs/radar_runner.log',
        log_tail: ['line a', 'line b'],
      }),
    );
    globalThis.fetch = fetchMock;
    const s = await api.getRadarStatus(15);
    expect(s.alive).toBe(true);
    expect(s.pid).toBe(28296);
    expect(s.log_tail).toEqual(['line a', 'line b']);
    expect(fetchMock.mock.calls[0]![0]).toContain('log_lines=15');
  });

  it('startRadar issues a POST and parses the action result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ ok: true, alive: true, pid: 42, message: 'spawned' }),
    );
    globalThis.fetch = fetchMock;
    const r = await api.startRadar();
    expect(r.ok).toBe(true);
    expect(r.pid).toBe(42);
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
  });

  it('stopRadar handles "not running" cleanly', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ ok: true, killed: false, pid: 0, message: 'not running (no pid file)' }),
    );
    const r = await api.stopRadar();
    expect(r.ok).toBe(true);
    expect(r.killed).toBe(false);
  });

  it('rejects malformed status payload', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ wrong: 'shape' }));
    await expect(api.getRadarStatus()).rejects.toBeInstanceOf(ApiError);
  });

  it('wraps 503 start failure as ApiError', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{"error":"spawn failed"}', { status: 503 }),
    );
    await expect(api.startRadar()).rejects.toBeInstanceOf(ApiError);
  });
});
