import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './playbookApi';

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

describe('playbookApi', () => {
  const entry = {
    id: 'pb_1',
    category: 'coding',
    content: 'Use rg before slower recursive scans.',
    status: 'pending',
    rationale: 'Faster in local repos.',
    source: 'curator_propose',
    source_turn: 7,
    source_session: 'test',
    tags: ['search'],
    helpful: 0,
    harmful: 0,
    created_at: '2026-05-24T00:00:00Z',
    decided_at: null,
    decision_note: null,
  };

  it('fetchPlaybook parses entries and stats', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        entries: [entry],
        stats: {
          path: 'memory/playbook.json',
          total: 1,
          status: { active: 0, pending: 1, rejected: 0 },
          active_categories: {},
        },
      }),
    );
    globalThis.fetch = fetchMock;

    const data = await api.fetchPlaybook('pending');

    expect(data.entries[0]!.id).toBe('pb_1');
    expect(data.stats.status.pending).toBe(1);
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/playbook?status=pending');
  });

  it('decidePlaybookEntry posts the review decision', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true, message: 'approved' }));
    globalThis.fetch = fetchMock;

    const result = await api.decidePlaybookEntry('pb_1', 'accept', 'approved');

    expect(result.ok).toBe(true);
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/playbook/pb_1/decide');
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1].body))).toEqual({
      decision: 'accept',
      note: 'approved',
    });
  });

  it('rejects malformed playbook payloads', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ entries: [{ id: 'missing' }] }));
    await expect(api.fetchPlaybook()).rejects.toBeInstanceOf(ApiError);
  });
});
