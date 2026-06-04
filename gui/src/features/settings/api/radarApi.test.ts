import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './radarApi';

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

describe('radarApi', () => {
  const statusPayload = {
    alive: true,
    pid: 28296,
    pid_source: 'pid_file',
    lock_port: 45765,
    config: {
      ready: true,
      missing: [],
      warnings: ['Grok 实时源未配置；GitHub/HN 源仍可运行'],
      feishu: {
        app_id_configured: true,
        app_secret_configured: true,
        notify_to: 'ou_abc',
        notify_to_configured: true,
      },
      sources: {
        github: true,
        hn: true,
        grok: false,
        grok_model: 'grok-4-fast-reasoning',
        tavily: true,
      },
      quiet_hours: '22-8',
      watchlist_count: 2,
    },
    log_path: 'temp/logs/radar_runner.log',
    log_tail: ['line a', 'line b'],
  };

  it('getRadarStatus parses payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(statusPayload),
    );
    globalThis.fetch = fetchMock;
    const s = await api.getRadarStatus(15);
    expect(s.alive).toBe(true);
    expect(s.pid).toBe(28296);
    expect(s.config?.ready).toBe(true);
    expect(s.log_tail).toEqual(['line a', 'line b']);
    expect(fetchMock.mock.calls[0]![0]).toContain('log_lines=15');
  });

  it('getRadarConfig parses masked config and status', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        config: {
          feishu_app_id: '***',
          feishu_app_secret: '***',
          notify_to: 'ou_abc',
          quiet_hours: '22-8',
          watchlist: ['openai/codex', 'anthropics/claude-code'],
          grok_api_key: '***',
          grok_url: 'https://api.x.ai/v1',
          grok_model: 'grok-4-fast-reasoning',
          tavily_api_key: '',
          tavily_url: 'https://api.tavily.com/search',
        },
        status: statusPayload,
      }),
    );
    globalThis.fetch = fetchMock;

    const r = await api.getRadarConfig();

    expect(r.config.feishu_app_id).toBe('***');
    expect(r.config.watchlist).toEqual(['openai/codex', 'anthropics/claude-code']);
    expect(r.config.grok_url).toBe('https://api.x.ai/v1');
    expect(r.config.tavily_url).toBe('https://api.tavily.com/search');
    expect(r.status.config?.feishu.notify_to).toBe('ou_abc');
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/radar/config');
  });

  it('saveRadarConfig issues PUT with radar fields', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        config: {
          feishu_app_id: '***',
          feishu_app_secret: '***',
          notify_to: 'ou_new',
          quiet_hours: 'off',
          watchlist: ['openai/codex'],
          grok_api_key: '***',
          grok_url: 'https://proxy.example/grok/v1',
          grok_model: 'grok-4-fast-reasoning',
          tavily_api_key: '***',
          tavily_url: 'https://proxy.example/tavily/search',
        },
        status: statusPayload,
      }),
    );
    globalThis.fetch = fetchMock;

    const r = await api.saveRadarConfig({
      notify_to: 'ou_new',
      watchlist: ['openai/codex'],
      grok_url: 'https://proxy.example/grok/v1',
      tavily_url: 'https://proxy.example/tavily',
    });

    expect(r.config.notify_to).toBe('ou_new');
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/radar/config');
    expect(fetchMock.mock.calls[0]![1].method).toBe('PUT');
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1].body))).toEqual({
      notify_to: 'ou_new',
      watchlist: ['openai/codex'],
      grok_url: 'https://proxy.example/grok/v1',
      tavily_url: 'https://proxy.example/tavily',
    });
  });

  it('startRadar issues a POST and parses the action result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ ok: true, alive: true, pid: 42, message: 'spawned' }),
    );
    globalThis.fetch = fetchMock;
    const r = await api.startRadar();
    expect(r.ok).toBe(true);
    expect(r.pid).toBe(42);
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
  });

  it('stopRadar handles "not running" cleanly', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ ok: true, killed: false, pid: 0, message: 'not running (no pid file)' }),
    );
    const r = await api.stopRadar();
    expect(r.ok).toBe(true);
    expect(r.killed).toBe(false);
  });

  it('rejects malformed status payload', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ wrong: 'shape' }));
    await expect(api.getRadarStatus()).rejects.toBeInstanceOf(ApiError);
  });

  it('wraps 503 start failure as ApiError', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{"error":"spawn failed"}', { status: 503 }),
    );
    await expect(api.startRadar()).rejects.toBeInstanceOf(ApiError);
  });
});
