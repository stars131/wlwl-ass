import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import { activityResponseSchema, type ActivityResponse } from '../types';

async function request<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  const url = `${getApiBase()}${path}`;
  const res = await fetch(url, { headers: apiHeaders() });
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

export function fetchActivityRecent(limit: number = 200): Promise<ActivityResponse> {
  return request(`/api/activity?limit=${limit}`, activityResponseSchema);
}

// ── Trajectory export (#32) ──────────────────────────────────────────

export const trajectoryRunSummarySchema = z.object({
  run_id: z.string(),
  n_turns: z.number(),
  skills_used: z.array(z.string()),
  outcomes: z.record(z.string(), z.number()),
});
export type TrajectoryRunSummary = z.infer<typeof trajectoryRunSummarySchema>;

export const trajectoryExportSchema = z.object({
  path: z.string(),
  count: z.number(),
  runs: z.array(trajectoryRunSummarySchema),
});
export type TrajectoryExport = z.infer<typeof trajectoryExportSchema>;

export function exportTrajectory(
  opts: { max_blob_chars?: number; include_args?: boolean } = {},
): Promise<TrajectoryExport> {
  const params = new URLSearchParams();
  if (opts.max_blob_chars != null) params.set('max_blob_chars', String(opts.max_blob_chars));
  if (opts.include_args != null) params.set('include_args', opts.include_args ? '1' : '0');
  const qs = params.toString();
  return request(`/api/trajectory/export${qs ? `?${qs}` : ''}`, trajectoryExportSchema);
}
