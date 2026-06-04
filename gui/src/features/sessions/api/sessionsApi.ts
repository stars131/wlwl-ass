/**
 * Typed wrappers over the Phase 1 endpoints in launcher/api_server.py.
 */
import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import {
  chatMessagesSchema,
  configsListSchema,
  profilesStateSchema,
  projectSchema,
  projectsListSchema,
  type ApiConfig,
  type ChatMessages,
  type Project,
  type RequestMode,
  type ProfilesState,
  type ProjectsList,
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

const singleProjectSchema = z.object({ project: projectSchema });

export function listProjects(): Promise<ProjectsList> {
  return request('/api/projects', projectsListSchema);
}

export async function createProject(name: string): Promise<Project> {
  const data = await request('/api/projects', singleProjectSchema, {
    method: 'POST',
    body: { name },
  });
  return data.project;
}

export async function deleteProject(id: string): Promise<void> {
  await request(`/api/projects/${encodeURIComponent(id)}`, z.object({ deleted: z.string() }), {
    method: 'DELETE',
  });
}

export async function startProject(id: string, opts: { resume_task_id?: string } = {}): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}/start`,
    singleProjectSchema,
    { method: 'POST', body: opts },
  );
  return data.project;
}

export async function stopProject(id: string): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}/stop`,
    singleProjectSchema,
    { method: 'POST', body: {} },
  );
  return data.project;
}

export async function renameProject(id: string, name: string): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}`,
    singleProjectSchema,
    { method: 'PATCH', body: { name } },
  );
  return data.project;
}

export async function setProjectAutonomous(id: string, autonomousEnabled: boolean): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}`,
    singleProjectSchema,
    { method: 'PATCH', body: { autonomous_enabled: autonomousEnabled } },
  );
  return data.project;
}

export async function pinProject(id: string, pinned: boolean): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}/pin`,
    singleProjectSchema,
    { method: 'POST', body: { pinned } },
  );
  return data.project;
}

export async function activateProject(id: string): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}/activate`,
    singleProjectSchema,
    { method: 'POST', body: {} },
  );
  return data.project;
}

const openProjectSchema = z.object({
  opened: z.boolean(),
  url: z.string(),
});
export async function openProjectInBrowser(id: string): Promise<{ url: string }> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}/open`,
    openProjectSchema,
    { method: 'POST', body: {} },
  );
  return { url: data.url };
}

export function listProjectMessages(id: string): Promise<ChatMessages> {
  return request(`/api/projects/${encodeURIComponent(id)}/messages`, chatMessagesSchema);
}

export function sendProjectMessage(
  id: string,
  text: string,
  mode: RequestMode = 'auto',
): Promise<ChatMessages> {
  return request(`/api/projects/${encodeURIComponent(id)}/messages`, chatMessagesSchema, {
    method: 'POST',
    body: { text, mode },
  });
}

export function abortProjectMessage(id: string): Promise<ChatMessages> {
  return request(`/api/projects/${encodeURIComponent(id)}/messages/abort`, chatMessagesSchema, {
    method: 'POST',
    body: {},
  });
}

// ── Auto-checkpoint resume browser (#O) ──────────────────────────────

export const projectCheckpointSchema = z.object({
  task_id: z.string(),
  checkpoint_count: z.number().optional(),
  latest_saved_at: z.string().nullable().optional(),
  latest_note: z.string().optional().default(''),
  latest_id: z.string().optional(),
});
export type ProjectCheckpoint = z.infer<typeof projectCheckpointSchema>;

const projectCheckpointsSchema = z.object({ checkpoints: z.array(projectCheckpointSchema) });

export async function listProjectCheckpoints(id: string): Promise<{ checkpoints: ProjectCheckpoint[] }> {
  return request(
    `/api/projects/${encodeURIComponent(id)}/checkpoints`,
    projectCheckpointsSchema,
  );
}

export async function setProjectLlm(
  id: string,
  payload: { config_name?: string; profile_name?: string; llm_no?: number },
): Promise<Project> {
  const data = await request(
    `/api/projects/${encodeURIComponent(id)}/llm`,
    singleProjectSchema,
    { method: 'PUT', body: payload },
  );
  return data.project;
}

export async function listApiConfigs(): Promise<ApiConfig[]> {
  const data = await request('/api/configs', configsListSchema);
  return data.configs;
}

export function getProfiles(): Promise<ProfilesState> {
  return request('/api/profiles', profilesStateSchema);
}
