import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './onboardingApi';

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

function makeStatus(overrides: Record<string, unknown> = {}) {
  return {
    needs_setup: true,
    reason: 'No usable LLM config found',
    env_recognized: false,
    has_mykey: false,
    providers: ['openai', 'anthropic'],
    ...overrides,
  };
}

describe('onboardingApi.fetchOnboardingStatus', () => {
  it('parses needs_setup=true', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(makeStatus()));
    const data = await api.fetchOnboardingStatus();
    expect(data.needs_setup).toBe(true);
    expect(data.providers).toContain('openai');
  });

  it('parses needs_setup=false (already configured)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse(makeStatus({ needs_setup: false, has_mykey: true, reason: '' })),
    );
    const data = await api.fetchOnboardingStatus();
    expect(data.needs_setup).toBe(false);
    expect(data.has_mykey).toBe(true);
  });

  it('rejects when shape is wrong', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ providers: 'not an array' }));
    await expect(api.fetchOnboardingStatus()).rejects.toBeInstanceOf(ApiError);
  });
});

describe('onboardingApi.saveOnboarding', () => {
  it('POSTs the payload as JSON and returns the new status', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(makeStatus({ needs_setup: false })));
    globalThis.fetch = fetchMock;
    const data = await api.saveOnboarding({
      provider: 'openai',
      apikey: 'sk-test',
      base_url: '',
      model: '',
    });
    expect(data.needs_setup).toBe(false);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toContain('/api/onboarding/save');
    expect((init as RequestInit).method).toBe('POST');
    const body = JSON.parse((init as RequestInit).body as string) as Record<string, string>;
    expect(body.provider).toBe('openai');
    expect(body.apikey).toBe('sk-test');
  });

  it('surfaces 400 validation errors as ApiError', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: 'apikey is required' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }),
    );
    await expect(
      api.saveOnboarding({ provider: 'openai', apikey: '', base_url: '', model: '' }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
