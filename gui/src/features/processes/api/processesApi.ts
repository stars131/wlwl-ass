import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { getApiBase } from '@/lib/env';

async function request<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  init?: Omit<RequestInit, 'body'> & { body?: unknown },
): Promise<z.output<S>> {
  const url = `${getApiBase()}${path}`;
  const { body, ...restInit } = init ?? {};
  const fetchInit: RequestInit = {
    headers: { 'content-type': 'application/json' },
    ...restInit,
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

export const processEntrySchema = z.object({
  label: z.string(),
  pid: z.number(),
  kind: z.string(),
  cmd: z.string().optional().default(''),
  started_at: z.string(),
  meta: z.record(z.string(), z.any()).optional().default({}),
  alive: z.boolean(),
});
export type ProcessEntry = z.infer<typeof processEntrySchema>;

const processesListSchema = z.object({ processes: z.array(processEntrySchema) });

export function listProcesses(): Promise<{ processes: ProcessEntry[] }> {
  return request('/api/processes', processesListSchema);
}

const killResultSchema = z.object({
  ok: z.boolean(),
  message: z.string().optional().default(''),
});

export function killProcess(target: { pid?: number; label?: string; force?: boolean }): Promise<{ ok: boolean; message: string }> {
  return request('/api/processes/kill', killResultSchema, {
    method: 'POST',
    body: target,
  });
}

const cleanupSchema = z.object({ removed: z.number() });
export function cleanupDeadProcesses(): Promise<{ removed: number }> {
  return request('/api/processes/cleanup', cleanupSchema, { method: 'POST', body: {} });
}
