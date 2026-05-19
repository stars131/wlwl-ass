import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

// ── schemas ──────────────────────────────────────────────────────────

export const radarStatusSchema = z.object({
  alive: z.boolean(),
  pid: z.number(),
  log_path: z.string(),
  log_tail: z.array(z.string()),
});

export const radarActionSchema = z.object({
  ok: z.boolean(),
  alive: z.boolean().optional(),
  killed: z.boolean().optional(),
  pid: z.number(),
  message: z.string(),
});

export type RadarStatus = z.infer<typeof radarStatusSchema>;
export type RadarAction = z.infer<typeof radarActionSchema>;

// ── fetch wrapper ────────────────────────────────────────────────────

async function request<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  init?: Omit<RequestInit, 'body'> & { body?: unknown },
): Promise<z.output<S>> {
  const url = `${getApiBase()}${path}`;
  const { body, ...rest } = init ?? {};
  const fetchInit: RequestInit = {
    ...rest,
    headers: apiHeaders(rest.headers),
  };
  if (body !== undefined) {
    fetchInit.body = typeof body === 'string' ? body : JSON.stringify(body);
  }
  const res = await fetch(url, fetchInit);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new ApiError(`HTTP ${res.status} on ${path}: ${text.slice(0, 200)}`, res.status, url);
  }
  const json = (await res.json()) as unknown;
  const parsed = schema.safeParse(json);
  if (!parsed.success) {
    throw new ApiError(`Invalid response from ${path}: ${parsed.error.message}`, res.status, url);
  }
  return parsed.data;
}

// ── radar API ────────────────────────────────────────────────────────

export function getRadarStatus(logLines = 20): Promise<RadarStatus> {
  return request(`/api/radar/status?log_lines=${logLines}`, radarStatusSchema);
}

export function startRadar(): Promise<RadarAction> {
  return request('/api/radar/start', radarActionSchema, { method: 'POST' });
}

export function stopRadar(): Promise<RadarAction> {
  return request('/api/radar/stop', radarActionSchema, { method: 'POST' });
}
