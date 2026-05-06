/**
 * Typed HTTP client for the Python `launcher.api_server` backend.
 *
 * Every endpoint is wrapped here with a Zod schema so the React layer never
 * trusts arbitrary JSON. Phase 0 ships only `/api/health` and `/api/version`;
 * subsequent phases extend this file with `fetchProjects`, `fetchBots`, etc.
 */
import { z } from 'zod';

import { getApiBase } from './env';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly url: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  const url = `${getApiBase()}${path}`;
  const res = await fetch(url, {
    headers: { 'content-type': 'application/json' },
    ...init,
  });
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

// ─── Schemas ──────────────────────────────────────────────────────────

export const healthSchema = z.object({
  status: z.literal('ok'),
  uptime_s: z.number().nonnegative(),
  pid: z.number().int(),
});
export type Health = z.infer<typeof healthSchema>;

export const versionSchema = z.object({
  version: z.string(),
  api: z.string(),
  python: z.string(),
  platform: z.string(),
});
export type Version = z.infer<typeof versionSchema>;

// ─── Endpoints ────────────────────────────────────────────────────────

export function fetchHealth(): Promise<Health> {
  return request('/api/health', healthSchema);
}

export function fetchVersion(): Promise<Version> {
  return request('/api/version', versionSchema);
}
