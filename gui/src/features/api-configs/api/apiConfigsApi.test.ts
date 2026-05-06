import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './apiConfigsApi';

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

describe('apiConfigsApi', () => {
  it('listConfigs parses', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ configs: [{ kind: 'native_oai', name: 'gpt' }] }),
    );
    const data = await api.listConfigs();
    expect(data.configs[0]!.name).toBe('gpt');
  });

  it('saveConfigs PUT', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ configs: [] }));
    globalThis.fetch = fetchMock;
    await api.saveConfigs([{ kind: 'native_oai', name: 'x' }]);
    expect(fetchMock.mock.calls[0]![1].method).toBe('PUT');
    const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string);
    expect(body.configs[0].name).toBe('x');
  });

  it('setActiveProfile null clears active', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ active: null, profiles: {} }));
    globalThis.fetch = fetchMock;
    const r = await api.setActiveProfile(null);
    expect(r.active).toBeNull();
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({ name: null });
  });

  it('upsertProfile sends members array', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ active: null, profiles: { p: ['a'] } }),
    );
    globalThis.fetch = fetchMock;
    const r = await api.upsertProfile('p', ['a']);
    expect(r.profiles.p).toEqual(['a']);
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
  });

  it('renameProfile uses PATCH and encoded name', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ active: null, profiles: { 'b/c': [] } }));
    globalThis.fetch = fetchMock;
    await api.renameProfile('a/b', 'b/c');
    const url = fetchMock.mock.calls[0]![0] as string;
    expect(url.endsWith('/api/profiles/a%2Fb')).toBe(true);
    expect(fetchMock.mock.calls[0]![1].method).toBe('PATCH');
  });

  it('deleteProfile DELETE', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ active: null, profiles: {} }));
    globalThis.fetch = fetchMock;
    await api.deleteProfile('z');
    expect(fetchMock.mock.calls[0]![1].method).toBe('DELETE');
  });

  it('rejects malformed', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ wrong: 'shape' }));
    await expect(api.listConfigs()).rejects.toBeInstanceOf(ApiError);
  });
});
