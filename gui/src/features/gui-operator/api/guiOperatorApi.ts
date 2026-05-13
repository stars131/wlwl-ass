import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import {
  guiRunResponseSchema,
  guiRunsResponseSchema,
  guiSidecarStatusSchema,
  type GuiRun,
  type GuiRunsResponse,
  type GuiSidecarStatus,
  type StartGuiRunInput,
} from '../types';

async function request<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  init?: Omit<RequestInit, 'body'> & { body?: unknown },
): Promise<z.output<S>> {
  const url = `${getApiBase()}${path}`;
  const { body, ...restInit } = init ?? {};
  const fetchInit: RequestInit = {
    ...restInit,
    headers: apiHeaders(restInit.headers),
  };
  if (body !== undefined) {
    fetchInit.body = typeof body === 'string' ? body : JSON.stringify(body);
  }
  const res = await fetch(url, fetchInit);
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(`HTTP ${res.status} on ${path}: ${body.slice(0, 200)}`, res.status, url);
  }
  const json = (await res.json()) as unknown;
  const parsed = schema.safeParse(json);
  if (!parsed.success) {
    throw new ApiError(`Invalid response from ${path}: ${parsed.error.message}`, res.status, url);
  }
  return parsed.data;
}

export function listGuiRuns(): Promise<GuiRunsResponse> {
  return request('/api/gui_operator/runs', guiRunsResponseSchema);
}

export async function startGuiRun(input: StartGuiRunInput): Promise<GuiRun> {
  const data = await request('/api/gui_operator/runs', guiRunResponseSchema, {
    method: 'POST',
    body: input,
  });
  return data.run;
}

export async function pauseGuiRun(runId: string): Promise<GuiRun> {
  const data = await request(
    `/api/gui_operator/runs/${encodeURIComponent(runId)}/pause`,
    guiRunResponseSchema,
    {
      method: 'POST',
      body: {},
    },
  );
  return data.run;
}

export async function resumeGuiRun(runId: string): Promise<GuiRun> {
  const data = await request(
    `/api/gui_operator/runs/${encodeURIComponent(runId)}/resume`,
    guiRunResponseSchema,
    {
      method: 'POST',
      body: {},
    },
  );
  return data.run;
}

export async function stopGuiRun(runId: string): Promise<GuiRun> {
  const data = await request(
    `/api/gui_operator/runs/${encodeURIComponent(runId)}/stop`,
    guiRunResponseSchema,
    {
      method: 'POST',
      body: {},
    },
  );
  return data.run;
}

export function getGuiSidecarStatus(): Promise<GuiSidecarStatus> {
  return request('/api/gui_operator/sidecar', guiSidecarStatusSchema);
}
