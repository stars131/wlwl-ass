import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from './guiOperatorApi';

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

const run = {
  run_id: 'run-1',
  instruction: 'click center',
  max_loop: 3,
  loop_wait: 1,
  dry_run: true,
  backend: 'auto',
  all_screens: false,
  include_base64: true,
  status: 'dry_run',
  run_dir: 'temp/gui_runs/run-1',
  error: '',
  created_at: 1,
  updated_at: 2,
  steps: [{ step: 1, status: 'dry_run', parsed: { start_coords: [1, 2] } }],
  result: null,
};

describe('guiOperatorApi', () => {
  it('listGuiRuns parses runs', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ runs: [run] }));
    const data = await api.listGuiRuns();
    expect(data.runs[0]!.run_id).toBe('run-1');
    expect(data.runs[0]!.steps[0]!.status).toBe('dry_run');
  });

  it('startGuiRun posts a JSON body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ run }));
    globalThis.fetch = fetchMock;
    const data = await api.startGuiRun({
      instruction: 'click center',
      max_loop: 3,
      loop_wait: 1,
      dry_run: true,
      backend: 'auto',
      all_screens: false,
      include_base64: true,
    });
    expect(data.run_id).toBe('run-1');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/gui_operator/runs'),
      expect.objectContaining({ method: 'POST', body: expect.stringContaining('click center') }),
    );
  });

  it('getGuiSidecarStatus parses optional sidecar state', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({
      configured: false,
      url: '',
      node_available: true,
      sidecar_package: 'gui/ui-tars-sidecar/package.json',
      mode: 'optional',
      message: 'optional',
    }));
    const data = await api.getGuiSidecarStatus();
    expect(data.mode).toBe('optional');
    expect(data.node_available).toBe(true);
  });
});
