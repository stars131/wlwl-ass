import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './doctorApi';

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

function makeReport(overrides: Record<string, unknown> = {}) {
  return {
    project_root: '/x/repo',
    summary: { ok: 5, warn: 1, fail: 0, info: 2 },
    checks: [
      { id: 'python.version', title: 'Python 3.12', severity: 'ok', detail: '', fix: '' },
      { id: 'web.node', title: 'Web UI: node 20.11.1', severity: 'ok', detail: '', fix: '' },
    ],
    ...overrides,
  };
}

describe('doctorApi.fetchDoctor', () => {
  it('parses an ok report', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(makeReport()));
    const data = await api.fetchDoctor();
    expect(data.summary.ok).toBe(5);
    expect(data.checks).toHaveLength(2);
    expect(data.checks[1]!.id).toBe('web.node');
  });

  it('does not pass network query param by default', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(makeReport()));
    globalThis.fetch = fetchMock;
    await api.fetchDoctor();
    const url = fetchMock.mock.calls[0]![0] as string;
    expect(url).toContain('/api/doctor');
    expect(url).not.toContain('network=1');
  });

  it('passes network=1 when requested', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(makeReport()));
    globalThis.fetch = fetchMock;
    await api.fetchDoctor(true);
    const url = fetchMock.mock.calls[0]![0] as string;
    expect(url).toContain('network=1');
  });

  it('rejects on bad shape', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ checks: 'not array' }));
    await expect(api.fetchDoctor()).rejects.toBeInstanceOf(ApiError);
  });

  it('coerces missing severity counts to 0', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        project_root: '/x',
        summary: { ok: 1 },
        checks: [],
      }),
    );
    const data = await api.fetchDoctor();
    expect(data.summary.fail).toBe(0);
    expect(data.summary.warn).toBe(0);
  });
});
