/**
 * Bot credentials feature — read/write whitelist of bot fields via
 * /api/credentials. Lets users edit fs_app_id / tg_bot_token / etc. in
 * the GUI without opening ~/.wlwl-ass/config.json by hand.
 */
import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

const credValueSchema = z.union([z.string(), z.array(z.string()), z.array(z.number())]);

export const credentialsResponseSchema = z.object({
  fields: z.record(z.string(), z.array(z.string())),
  values: z.record(z.string(), z.record(z.string(), credValueSchema)),
});
export type CredentialsResponse = z.infer<typeof credentialsResponseSchema>;
export type CredValue = z.infer<typeof credValueSchema>;

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

export function getCredentials(): Promise<CredentialsResponse> {
  return request('/api/credentials', credentialsResponseSchema);
}

export function patchCredentials(
  patch: Record<string, CredValue>,
): Promise<CredentialsResponse> {
  return request('/api/credentials', credentialsResponseSchema, {
    method: 'PUT',
    body: patch,
  });
}
