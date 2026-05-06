import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './tokenUsageApi';

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

function makeSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    totals: {
      input: 0,
      output: 0,
      cache_creation: 0,
      cache_read: 0,
      calls: 0,
    },
    by_mode: {},
    since: '2026-05-03T10:00:00Z',
    last_ts: '',
    cache_hit_rate: null,
    recent: [],
    ...overrides,
  };
}

describe('tokenUsageApi.fetchTokenUsage', () => {
  it('parses an empty snapshot', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(makeSnapshot()));
    const data = await api.fetchTokenUsage();
    expect(data.totals.calls).toBe(0);
    expect(data.cache_hit_rate).toBeNull();
  });

  it('parses a populated snapshot with mode breakdown', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse(
        makeSnapshot({
          totals: { input: 500, output: 100, cache_creation: 200, cache_read: 800, calls: 4 },
          by_mode: {
            messages: { input: 500, output: 100, cache_creation: 200, cache_read: 800, calls: 4 },
          },
          cache_hit_rate: 0.533,
          last_ts: '2026-05-03T10:01:00Z',
        }),
      ),
    );
    const data = await api.fetchTokenUsage();
    expect(data.totals.input).toBe(500);
    expect(data.by_mode.messages!.calls).toBe(4);
    expect(data.cache_hit_rate).toBeCloseTo(0.533);
  });

  it('rejects when shape is wrong', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ totals: { input: 'not a number' } }),
    );
    await expect(api.fetchTokenUsage()).rejects.toBeInstanceOf(ApiError);
  });

  it('hits the /api/token_usage endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(makeSnapshot()));
    globalThis.fetch = fetchMock;
    await api.fetchTokenUsage();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/token_usage'),
      expect.objectContaining({ headers: expect.any(Object) }),
    );
  });
});

describe('tokenUsageApi.resetTokenUsage', () => {
  it('POSTs to /api/token_usage/reset and returns the cleared snapshot', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(makeSnapshot()));
    globalThis.fetch = fetchMock;
    const data = await api.resetTokenUsage();
    expect(data.totals.calls).toBe(0);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/token_usage/reset');
    expect((init as RequestInit).method).toBe('POST');
  });
});
