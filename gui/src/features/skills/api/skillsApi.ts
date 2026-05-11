import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import { skillsResponseSchema, type SkillsResponse } from '../types';

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

export function fetchSkills(): Promise<SkillsResponse> {
  return request('/api/skills', skillsResponseSchema);
}

// ── Skill self-improve proposals (#6) ────────────────────────────────

export const proposalSchema = z.object({
  id: z.string(),
  skill_id: z.string(),
  skill_path: z.string().nullable().optional(),
  diff: z.string(),
  reason: z.string().optional().default(''),
  status: z.enum(['open', 'accepted', 'rejected']),
  proposed_at: z.string(),
  decided_at: z.string().nullable().optional(),
  decision_note: z.string().nullable().optional(),
  backup_path: z.string().optional(),
});
export type SkillProposal = z.infer<typeof proposalSchema>;

const proposalsListSchema = z.object({ proposals: z.array(proposalSchema) });
const proposalActionSchema = z.object({
  ok: z.boolean(),
  message: z.string().optional().default(''),
});

export function fetchProposals(): Promise<{ proposals: SkillProposal[] }> {
  return request('/api/skills/proposals', proposalsListSchema);
}

export function decideProposal(
  id: string,
  decision: 'accept' | 'reject',
): Promise<{ ok: boolean; message: string }> {
  return request(
    `/api/skills/proposals/${encodeURIComponent(id)}/decide`,
    proposalActionSchema,
    { method: 'POST', body: { decision } },
  );
}

const proposalPreviewSchema = z.object({
  ok: z.boolean(),
  mode: z.enum(['unified-diff', 'whole-file']).optional(),
  before: z.string().optional(),
  after: z.string().optional(),
  message: z.string().optional(),
  error: z.string().optional(),
  skill_path: z.string().nullable().optional(),
});
export type ProposalPreview = z.infer<typeof proposalPreviewSchema>;

export function previewProposal(id: string): Promise<ProposalPreview> {
  return request(
    `/api/skills/proposals/${encodeURIComponent(id)}/preview`,
    proposalPreviewSchema,
  );
}
