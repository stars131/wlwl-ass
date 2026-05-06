import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { getApiBase } from '@/lib/env';

import { settingsResponseSchema, type Settings } from '../types';

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

export async function getSettings(): Promise<Settings> {
  const data = await request('/api/settings', settingsResponseSchema);
  return data.settings;
}

export async function patchSettings(patch: Partial<Settings>): Promise<Settings> {
  const data = await request('/api/settings', settingsResponseSchema, {
    method: 'PUT',
    body: patch,
  });
  return data.settings;
}
