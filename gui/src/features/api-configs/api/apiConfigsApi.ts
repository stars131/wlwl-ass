import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import {
  apiConfigsListSchema,
  profilesStateSchema,
  type ApiConfigEntry,
  type ApiConfigsList,
  type ProfilesState,
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

export function listConfigs(): Promise<ApiConfigsList> {
  return request('/api/configs', apiConfigsListSchema);
}

export function saveConfigs(configs: ApiConfigEntry[]): Promise<ApiConfigsList> {
  return request('/api/configs', apiConfigsListSchema, { method: 'PUT', body: { configs } });
}

export function getProfiles(): Promise<ProfilesState> {
  return request('/api/profiles', profilesStateSchema);
}

export function setActiveProfile(name: string | null): Promise<ProfilesState> {
  return request('/api/profiles/active', profilesStateSchema, { method: 'PUT', body: { name } });
}

export function upsertProfile(name: string, members: string[]): Promise<ProfilesState> {
  return request('/api/profiles', profilesStateSchema, {
    method: 'POST',
    body: { name, members },
  });
}

export function renameProfile(oldName: string, newName: string): Promise<ProfilesState> {
  return request(`/api/profiles/${encodeURIComponent(oldName)}`, profilesStateSchema, {
    method: 'PATCH',
    body: { new_name: newName },
  });
}

export function deleteProfile(name: string): Promise<ProfilesState> {
  return request(`/api/profiles/${encodeURIComponent(name)}`, profilesStateSchema, {
    method: 'DELETE',
  });
}

const llmTestResultSchema = z.object({
  ok: z.boolean(),
  status: z.number().optional(),
  latency_ms: z.number().optional(),
  sample: z.string().optional(),
  error: z.string().nullable().optional(),
});
export type LlmTestResult = z.infer<typeof llmTestResultSchema>;

export function testApiConfig(payload: {
  name?: string | undefined;
  kind?: string | undefined;
  apibase?: string | undefined;
  apikey?: string | undefined;
  model?: string | undefined;
}): Promise<LlmTestResult> {
  return request('/api/llm/test', llmTestResultSchema, {
    method: 'POST',
    body: payload,
  });
}
