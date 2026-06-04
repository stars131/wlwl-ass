import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

// ── schemas ──────────────────────────────────────────────────────────

export const radarStatusSchema = z.object({
  alive: z.boolean(),
  pid: z.number(),
  pid_source: z.string().optional(),
  lock_port: z.number().optional(),
  config: z
    .object({
      ready: z.boolean(),
      missing: z.array(z.string()),
      warnings: z.array(z.string()),
      feishu: z.object({
        app_id_configured: z.boolean(),
        app_secret_configured: z.boolean(),
        notify_to: z.string(),
        notify_to_configured: z.boolean(),
      }),
      sources: z.object({
        github: z.boolean(),
        hn: z.boolean(),
        grok: z.boolean(),
        grok_model: z.string(),
        tavily: z.boolean().optional(),
      }),
      quiet_hours: z.string(),
      watchlist_count: z.number(),
    })
    .optional(),
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

export const radarConfigSchema = z.object({
  feishu_app_id: z.string(),
  feishu_app_secret: z.string(),
  notify_to: z.string(),
  quiet_hours: z.string(),
  watchlist: z.array(z.string()),
  grok_api_key: z.string(),
  grok_url: z.string(),
  grok_model: z.string(),
  tavily_api_key: z.string(),
  tavily_url: z.string(),
});

export const radarConfigResponseSchema = z.object({
  config: radarConfigSchema,
  status: radarStatusSchema,
});

export type RadarConfig = z.infer<typeof radarConfigSchema>;
export type RadarConfigResponse = z.infer<typeof radarConfigResponseSchema>;

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

export function getRadarConfig(): Promise<RadarConfigResponse> {
  return request('/api/radar/config', radarConfigResponseSchema);
}

export function saveRadarConfig(patch: Partial<RadarConfig>): Promise<RadarConfigResponse> {
  return request('/api/radar/config', radarConfigResponseSchema, {
    method: 'PUT',
    body: patch,
  });
}

export function startRadar(): Promise<RadarAction> {
  return request('/api/radar/start', radarActionSchema, { method: 'POST' });
}

export function stopRadar(): Promise<RadarAction> {
  return request('/api/radar/stop', radarActionSchema, { method: 'POST' });
}
