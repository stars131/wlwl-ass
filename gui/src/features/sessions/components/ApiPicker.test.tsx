/**
 * Smoke tests for ApiPicker. Renders the component with mocked TanStack
 * Query state and verifies that:
 *   1. The dropdown renders profiles plus configs.
 *   2. Selecting a profile/config triggers setProjectLlm with the matching payload.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import { ApiPicker } from './ApiPicker';
import * as api from '../api/sessionsApi';
import type { Project } from '../types';

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p_x',
    name: 'demo',
    pinned: false,
    description: '',
    llm_no: 0,
    llm_config_name: '',
    llm_profile_name: '',
    running: false,
    ...overrides,
  };
}

const ORIGINAL_FETCH = globalThis.fetch;

function renderPicker(project: Project) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnMount: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ApiPicker project={project} />
    </QueryClientProvider>,
  );
}

describe('ApiPicker', () => {
  beforeEach(() => {
    (window as unknown as { __GA_API_BASE__?: string }).__GA_API_BASE__ = 'http://t.local';
  });
  afterEach(() => {
    globalThis.fetch = ORIGINAL_FETCH;
    vi.restoreAllMocks();
  });

  it('renders profiles and saves profile bindings on change', async () => {
    vi.spyOn(api, 'listApiConfigs').mockResolvedValue([
      { kind: 'native_oai', name: 'gpt-native', model: 'gpt-5', category: 'language', priority: 1 },
      { kind: 'native_claude', name: 'claude-relay-1', model: 'claude-opus-4-7', category: 'language', priority: 3 },
      { kind: 'native_claude', name: 'voice-relay', model: 'tts', category: 'voice', priority: 100 },
    ]);
    vi.spyOn(api, 'getProfiles').mockResolvedValue({
      active: 'claude-only',
      profiles: { 'claude-only': ['voice-relay', 'gpt-native', 'claude-relay-1'] },
    });
    const setSpy = vi
      .spyOn(api, 'setProjectLlm')
      .mockResolvedValue(makeProject({ llm_profile_name: 'claude-only' }));

    renderPicker(makeProject());

    await waitFor(() => {
      expect(screen.getByRole('option', { name: /Profile: claude-only/ })).toBeInTheDocument();
    });
    expect(
      screen.getByRole('option', {
        name: /claude-relay-1\(P3\).*gpt-native\(P1\).*voice-relay\(P100\)/,
      }),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'profile:claude-only' } });

    await waitFor(() => {
      expect(setSpy).toHaveBeenCalledWith('p_x', { profile_name: 'claude-only' });
    });
  });

  it('saves config bindings on change', async () => {
    vi.spyOn(api, 'listApiConfigs').mockResolvedValue([
      { kind: 'native_oai', name: 'gpt-native' },
      { kind: 'native_claude', name: 'claude-relay-1' },
    ]);
    vi.spyOn(api, 'getProfiles').mockResolvedValue({ active: null, profiles: {} });
    const setSpy = vi.spyOn(api, 'setProjectLlm').mockResolvedValue(makeProject());

    renderPicker(makeProject());

    await waitFor(() => {
      expect(screen.getByRole('option', { name: /gpt-native/ })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /claude-relay-1/ })).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'config:gpt-native' } });

    await waitFor(() => {
      expect(setSpy).toHaveBeenCalledWith('p_x', { config_name: 'gpt-native' });
    });
  });
});
