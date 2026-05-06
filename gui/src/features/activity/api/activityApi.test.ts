import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './activityApi';

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

describe('activityApi', () => {
  it('fetchActivityRecent parses an empty response', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ events: [], path: '/x/2026-05-03.jsonl' }));
    const data = await api.fetchActivityRecent(50);
    expect(data.events).toEqual([]);
    expect(data.path).toContain('2026-05-03');
  });

  it('fetchActivityRecent parses tool_start + tool_end + turn_end events', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        events: [
          { ts: '2026-05-03T10:00:00.000Z', phase: 'tool_start', turn: 1, tool: 'file_read', args: { path: 'x' } },
          { ts: '2026-05-03T10:00:00.500Z', phase: 'tool_end', turn: 1, tool: 'file_read', elapsed_s: 0.5 },
          { ts: '2026-05-03T10:00:01.000Z', phase: 'turn_end', turn: 1, summary: 'read x' },
        ],
        path: '/x.jsonl',
      }),
    );
    const data = await api.fetchActivityRecent();
    expect(data.events).toHaveLength(3);
    expect(data.events[0]!.phase).toBe('tool_start');
    expect(data.events[1]!.elapsed_s).toBe(0.5);
    expect(data.events[2]!.summary).toBe('read x');
  });

  it('rejects events with invalid phase', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        events: [{ ts: 'now', phase: 'mystery_phase' }],
        path: '/x.jsonl',
      }),
    );
    await expect(api.fetchActivityRecent()).rejects.toBeInstanceOf(ApiError);
  });

  it('passes the limit query param', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ events: [], path: '/x' }));
    globalThis.fetch = fetchMock;
    await api.fetchActivityRecent(42);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/activity?limit=42'),
      expect.objectContaining({ headers: expect.any(Object) }),
    );
  });
});
