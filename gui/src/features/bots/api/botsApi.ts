import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import {
  botActionResponseSchema,
  botBindingResponseSchema,
  botLogSchema,
  botsListSchema,
  llmOptionsSchema,
  type BotActionResponse,
  type BotBindingResponse,
  type BotLog,
  type BotsList,
  type LlmOptions,
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

export function listBots(): Promise<BotsList> {
  return request('/api/bots', botsListSchema);
}

export function startBot(key: string): Promise<BotActionResponse> {
  return request(`/api/bots/${encodeURIComponent(key)}/start`, botActionResponseSchema, {
    method: 'POST',
    body: {},
  });
}

export function stopBot(key: string): Promise<BotActionResponse> {
  return request(`/api/bots/${encodeURIComponent(key)}/stop`, botActionResponseSchema, {
    method: 'POST',
    body: {},
  });
}

export function restartBot(key: string): Promise<BotActionResponse> {
  return request(`/api/bots/${encodeURIComponent(key)}/restart`, botActionResponseSchema, {
    method: 'POST',
    body: {},
  });
}

export function getBotLog(key: string): Promise<BotLog> {
  return request(`/api/bots/${encodeURIComponent(key)}/log`, botLogSchema);
}

const installSdkSchema = z.object({
  ok: z.boolean(),
  returncode: z.number().optional(),
  packages: z.array(z.string()).optional(),
  log_path: z.string().optional(),
});
export type InstallSdkResult = z.infer<typeof installSdkSchema>;

export function installBotSdk(key: string): Promise<InstallSdkResult> {
  // Use a longer-than-default request — pip install can run 30-60s.
  return request(
    `/api/bots/${encodeURIComponent(key)}/install_sdk`,
    installSdkSchema,
    { method: 'POST', body: {} },
  );
}

export function getLlmOptions(): Promise<LlmOptions> {
  return request('/api/bots/llm_options', llmOptionsSchema);
}

export function setBotLlmBinding(key: string, binding: string): Promise<BotBindingResponse> {
  return request(
    `/api/bots/${encodeURIComponent(key)}/llm`,
    botBindingResponseSchema,
    { method: 'PATCH', body: { binding } },
  );
}
