/**
 * Smoke tests for ApiPicker. Renders the component with mocked TanStack
 * Query state and verifies that:
 *   1. The dropdown renders the configs visible in the active profile.
 *   2. Selecting a config triggers setProjectLlm with the chosen name.
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

  it('renders configs filtered by active profile and saves on change', async () => {
    vi.spyOn(api, 'listApiConfigs').mockResolvedValue([
      { kind: 'native_oai', name: 'gpt-native', model: 'gpt-5' },
      { kind: 'native_claude', name: 'claude-relay-1', model: 'claude-opus-4-7' },
      { kind: 'native_claude', name: 'claude-relay-2', model: 'claude-opus-4-7' },
    ]);
    vi.spyOn(api, 'getProfiles').mockResolvedValue({
      active: 'claude-only',
      profiles: { 'claude-only': ['claude-relay-1', 'claude-relay-2'] },
    });
    const setSpy = vi
      .spyOn(api, 'setProjectLlm')
      .mockResolvedValue(makeProject({ llm_config_name: 'claude-relay-2' }));

    renderPicker(makeProject());

    // After hooks resolve, only the two profile-member configs render
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /claude-relay-1/ })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /claude-relay-2/ })).toBeInTheDocument();
    });
    expect(screen.queryByRole('option', { name: /gpt-native/ })).toBeNull();

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'claude-relay-2' } });

    await waitFor(() => {
      expect(setSpy).toHaveBeenCalledWith('p_x', { config_name: 'claude-relay-2' });
    });
  });

  it('shows all configs when no profile is active', async () => {
    vi.spyOn(api, 'listApiConfigs').mockResolvedValue([
      { kind: 'native_oai', name: 'gpt-native' },
      { kind: 'native_claude', name: 'claude-relay-1' },
    ]);
    vi.spyOn(api, 'getProfiles').mockResolvedValue({ active: null, profiles: {} });
    vi.spyOn(api, 'setProjectLlm').mockResolvedValue(makeProject());

    renderPicker(makeProject());

    await waitFor(() => {
      expect(screen.getByRole('option', { name: /gpt-native/ })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /claude-relay-1/ })).toBeInTheDocument();
    });
  });
});
