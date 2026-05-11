import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './settingsApi';

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

function makeSettings(overrides: Record<string, unknown> = {}) {
  return {
    tg: false,
    qq: false,
    feishu: false,
    wecom: false,
    dingtalk: false,
    wechat: false,
    scheduler: true,
    llm_no: 0,
    permission_mode: 'auto',
    project_root: '',
    use_project_context: true,
    autonomous_enabled: false,
    ...overrides,
  };
}

describe('settingsApi', () => {
  it('getSettings unwraps the settings field and tolerates extra backend keys', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ settings: makeSettings({ scheduler: false, feishu: true }) }),
    );
    const s = await api.getSettings();
    expect(s.scheduler).toBe(false);
    expect(s.permission_mode).toBe('auto');
  });

  it('patchSettings PUT', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ settings: makeSettings({ scheduler: false }) }),
    );
    globalThis.fetch = fetchMock;
    const r = await api.patchSettings({ scheduler: false });
    expect(r.scheduler).toBe(false);
    expect(fetchMock.mock.calls[0]![1].method).toBe('PUT');
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({ scheduler: false });
  });

  it('rejects malformed payload', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ wrong: 'shape' }));
    await expect(api.getSettings()).rejects.toBeInstanceOf(ApiError);
  });
});
