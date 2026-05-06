import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './skillsApi';

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

describe('skillsApi.fetchSkills', () => {
  it('parses an empty catalogue', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ skills: [] }));
    const data = await api.fetchSkills();
    expect(data.skills).toEqual([]);
  });

  it('parses a skill with outcomes', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        skills: [
          {
            name: 'web_setup_sop',
            title: 'Web 工具链初始化执行 SOP',
            subtitle: 'First paragraph.',
            path: 'memory/web_setup_sop.md',
            size_bytes: 1234,
            mtime: '2026-05-03T10:00:00Z',
            outcomes: {
              ok: 4,
              max_turns: 1,
              exited: 0,
              other: 0,
              total: 5,
              last_seen: '2026-05-03T10:00:00Z',
              success_rate: 0.8,
            },
          },
        ],
      }),
    );
    const data = await api.fetchSkills();
    expect(data.skills).toHaveLength(1);
    expect(data.skills[0]!.name).toBe('web_setup_sop');
    expect(data.skills[0]!.outcomes?.ok).toBe(4);
    expect(data.skills[0]!.outcomes?.success_rate).toBeCloseTo(0.8);
  });

  it('parses a skill that has never been invoked (outcomes=null)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        skills: [
          {
            name: 'plan_sop',
            title: 'Plan SOP',
            subtitle: '',
            path: 'memory/plan_sop.md',
            size_bytes: 100,
            mtime: '2026-05-03T10:00:00Z',
            outcomes: null,
          },
        ],
      }),
    );
    const data = await api.fetchSkills();
    expect(data.skills[0]!.outcomes).toBeNull();
  });

  it('rejects when shape is wrong', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        skills: [{ name: 'x' }], // missing required fields
      }),
    );
    await expect(api.fetchSkills()).rejects.toBeInstanceOf(ApiError);
  });

  it('hits the /api/skills endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ skills: [] }));
    globalThis.fetch = fetchMock;
    await api.fetchSkills();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/skills'),
      expect.objectContaining({ headers: expect.any(Object) }),
    );
  });
});
