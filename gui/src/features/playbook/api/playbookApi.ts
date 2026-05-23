import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

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
    const bodyText = await res.text().catch(() => '');
    throw new ApiError(`HTTP ${res.status} on ${path}: ${bodyText.slice(0, 200)}`, res.status, url);
  }
  const json = (await res.json()) as unknown;
  const parsed = schema.safeParse(json);
  if (!parsed.success) {
    throw new ApiError(`Invalid response from ${path}: ${parsed.error.message}`, res.status, url);
  }
  return parsed.data;
}

export const playbookEntrySchema = z.object({
  id: z.string(),
  category: z.string(),
  content: z.string(),
  status: z.enum(['pending', 'active', 'rejected']),
  rationale: z.string().optional().default(''),
  source: z.string().optional().default(''),
  source_turn: z.number().nullable().optional(),
  source_session: z.string().optional().default(''),
  tags: z.array(z.string()).optional().default([]),
  helpful: z.number().optional().default(0),
  harmful: z.number().optional().default(0),
  created_at: z.string(),
  decided_at: z.string().nullable().optional(),
  decision_note: z.string().nullable().optional(),
});

export type PlaybookEntry = z.infer<typeof playbookEntrySchema>;

export const playbookStatsSchema = z.object({
  path: z.string(),
  total: z.number(),
  status: z
    .object({
      active: z.number().optional().default(0),
      pending: z.number().optional().default(0),
      rejected: z.number().optional().default(0),
    })
    .passthrough(),
  active_categories: z.record(z.number()).optional().default({}),
});

export type PlaybookStats = z.infer<typeof playbookStatsSchema>;

export const playbookListSchema = z.object({
  entries: z.array(playbookEntrySchema),
  stats: playbookStatsSchema,
});

export type PlaybookList = z.infer<typeof playbookListSchema>;

const playbookDecisionSchema = z.object({
  ok: z.boolean(),
  message: z.string(),
});

export type PlaybookDecision = z.infer<typeof playbookDecisionSchema>;

export function fetchPlaybook(status?: PlaybookEntry['status']): Promise<PlaybookList> {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  return request(`/api/playbook${query}`, playbookListSchema);
}

export function decidePlaybookEntry(
  id: string,
  decision: 'accept' | 'reject',
  note = '',
): Promise<PlaybookDecision> {
  return request(
    `/api/playbook/${encodeURIComponent(id)}/decide`,
    playbookDecisionSchema,
    { method: 'POST', body: { decision, note } },
  );
}
