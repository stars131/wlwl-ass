/**
 * Tests for sessions API client. Mocks fetch; no live server required.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';

import * as api from './sessionsApi';

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

function makeProject(overrides: Record<string, unknown> = {}) {
  return {
    id: 'p_x',
    name: 'demo',
    pinned: false,
    description: '',
    llm_no: 0,
    llm_config_name: '',
    llm_profile_name: '',
    ...overrides,
  };
}

describe('sessionsApi', () => {
  it('listProjects parses payload', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        projects: [makeProject(), makeProject({ id: 'p_y', name: 'y' })],
        active_id: 'p_x',
      }),
    );
    const data = await api.listProjects();
    expect(data.projects).toHaveLength(2);
    expect(data.active_id).toBe('p_x');
  });

  it('createProject returns the project', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ project: makeProject() }, 201));
    const project = await api.createProject('demo');
    expect(project.id).toBe('p_x');
  });

  it('setProjectLlm posts config_name', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ project: makeProject({ llm_config_name: 'gpt-native' }) }),
    );
    globalThis.fetch = fetchMock;
    const project = await api.setProjectLlm('p_x', { config_name: 'gpt-native' });
    expect(project.llm_config_name).toBe('gpt-native');

    const call = fetchMock.mock.calls[0]!;
    expect(call[0]).toBe('http://t.local/api/projects/p_x/llm');
    expect(call[1].method).toBe('PUT');
    expect(JSON.parse(call[1].body as string)).toEqual({ config_name: 'gpt-native' });
  });

  it('setProjectLlm posts profile_name', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ project: makeProject({ llm_profile_name: 'daily' }) }),
    );
    globalThis.fetch = fetchMock;
    const project = await api.setProjectLlm('p_x', { profile_name: 'daily' });
    expect(project.llm_profile_name).toBe('daily');

    const call = fetchMock.mock.calls[0]!;
    expect(call[0]).toBe('http://t.local/api/projects/p_x/llm');
    expect(call[1].method).toBe('PUT');
    expect(JSON.parse(call[1].body as string)).toEqual({ profile_name: 'daily' });
  });

  it('setProjectLlm posts llm_no', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ project: makeProject({ llm_no: 3 }) }),
    );
    globalThis.fetch = fetchMock;
    await api.setProjectLlm('p_x', { llm_no: 3 });
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({ llm_no: 3 });
  });

  it('setProjectAutonomous patches the session flag', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ project: makeProject({ autonomous_enabled: true }) }),
    );
    globalThis.fetch = fetchMock;
    const project = await api.setProjectAutonomous('p_x', true);

    expect(project.autonomous_enabled).toBe(true);
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/projects/p_x');
    expect(fetchMock.mock.calls[0]![1].method).toBe('PATCH');
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({
      autonomous_enabled: true,
    });
  });

  it('rejects unparseable response', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ wrong: 'shape' }));
    await expect(api.listProjects()).rejects.toBeInstanceOf(ApiError);
  });

  it('wraps non-2xx as ApiError', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response('boom', { status: 500 }));
    await expect(api.createProject('x')).rejects.toMatchObject({ status: 500 });
  });

  it('startProject hits the start endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ project: makeProject() }));
    globalThis.fetch = fetchMock;
    await api.startProject('p_x');
    const url = fetchMock.mock.calls[0]![0] as string;
    expect(url.endsWith('/api/projects/p_x/start')).toBe(true);
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
  });

  it('sendProjectMessage posts text and parses messages', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        running: true,
        messages: [{
          id: 'm_1',
          seq: 1,
          role: 'user',
          content: 'hello',
          status: 'done',
          created_at: '2026-01-01T00:00:00',
        }],
      }),
    );
    globalThis.fetch = fetchMock;
    const data = await api.sendProjectMessage('p_x', 'hello');

    expect(data.messages).toHaveLength(1);
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/projects/p_x/messages');
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({
      text: 'hello',
      mode: 'auto',
    });
  });

  it('sendProjectMessage can force task mode', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ running: true, messages: [] }));
    globalThis.fetch = fetchMock;
    await api.sendProjectMessage('p_x', 'run tests', 'task');

    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({
      text: 'run tests',
      mode: 'task',
    });
  });

  it('abortProjectMessage posts to the abort endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        running: true,
        messages: [{
          id: 'm_2',
          seq: 2,
          role: 'assistant',
          content: 'partial',
          status: 'aborted',
          created_at: '2026-01-01T00:00:01',
        }],
      }),
    );
    globalThis.fetch = fetchMock;
    const data = await api.abortProjectMessage('p_x');

    expect(data.messages[0]!.status).toBe('aborted');
    expect(fetchMock.mock.calls[0]![0]).toBe('http://t.local/api/projects/p_x/messages/abort');
    expect(fetchMock.mock.calls[0]![1].method).toBe('POST');
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body as string)).toEqual({});
  });

  it('listApiConfigs unwraps the configs array', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ configs: [{ kind: 'native_oai', name: 'gpt' }] }),
    );
    const configs = await api.listApiConfigs();
    expect(configs).toHaveLength(1);
    expect(configs[0]!.name).toBe('gpt');
  });
});
