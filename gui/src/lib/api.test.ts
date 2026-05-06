import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, fetchHealth, fetchVersion } from './api';

const ORIGINAL_FETCH = globalThis.fetch;

describe('api client', () => {
  beforeEach(() => {
    (window as unknown as { __GA_API_BASE__?: string }).__GA_API_BASE__ = 'http://test.local';
  });

  afterEach(() => {
    globalThis.fetch = ORIGINAL_FETCH;
    vi.restoreAllMocks();
  });

  it('parses /api/health response', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ status: 'ok', uptime_s: 12.3, pid: 4242 }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );
    const data = await fetchHealth();
    expect(data.status).toBe('ok');
    expect(data.uptime_s).toBeGreaterThan(0);
    expect(data.pid).toBe(4242);
  });

  it('rejects malformed responses', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), { status: 200 }),
    );
    await expect(fetchHealth()).rejects.toBeInstanceOf(ApiError);
  });

  it('wraps non-2xx as ApiError', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response('boom', { status: 503 }));
    await expect(fetchVersion()).rejects.toMatchObject({
      name: 'ApiError',
      status: 503,
    });
  });

  it('parses /api/version', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          version: '0.1.0',
          api: 'v1',
          python: '3.12.10',
          platform: 'win32',
        }),
        { status: 200 },
      ),
    );
    const data = await fetchVersion();
    expect(data.version).toBe('0.1.0');
    expect(data.api).toBe('v1');
  });
});
