import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './botsApi';

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

function makeBot(overrides: Record<string, unknown> = {}) {
  return {
    key: 'feishu',
    display_name: '飞书',
    script: 'fsapp.py',
    configured: true,
    missing_fields: [],
    sdk_installed: true,
    missing_modules: [],
    running_self: false,
    running_external: false,
    running: false,
    log_path: '/tmp/fsapp.log',
    ...overrides,
  };
}

describe('botsApi', () => {
  it('listBots parses payload', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ bots: [makeBot(), makeBot({ key: 'tg', display_name: 'Telegram' })] }),
    );
    const data = await api.listBots();
    expect(data.bots).toHaveLength(2);
    expect(data.bots[0]!.key).toBe('feishu');
  });

  it('startBot posts to /start', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ key: 'feishu', message: '已启动 (pid=123)' }),
    );
    globalThis.fetch = fetchMock;
    const r = await api.startBot('feishu');
    expect(r.message).toContain('已启动');
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/bots/feishu/start');
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
  });

  it('stopBot translates the message', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ key: 'tg', message: '已停止' }));
    const r = await api.stopBot('tg');
    expect(r.message).toBe('已停止');
  });

  it('getBotLog returns lines', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        key: 'feishu',
        path: '/tmp/fsapp.log',
        lines: ['line a', 'line b'],
        exists: true,
      }),
    );
    const log = await api.getBotLog('feishu');
    expect(log.exists).toBe(true);
    expect(log.lines).toEqual(['line a', 'line b']);
  });

  it('rejects malformed bot rows', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ bots: [{ key: 'x' }] }));
    await expect(api.listBots()).rejects.toBeInstanceOf(ApiError);
  });

  it('wraps 409 start failure as ApiError', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(new Response('{"error":"start_failed"}', { status: 409 }));
    await expect(api.startBot('feishu')).rejects.toMatchObject({ status: 409 });
  });
});
