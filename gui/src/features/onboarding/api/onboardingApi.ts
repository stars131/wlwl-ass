import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { apiHeaders, getApiBase } from '@/lib/env';

import {
  onboardingStatusSchema,
  type OnboardingSavePayload,
  type OnboardingStatus,
} from '../types';

async function request<T>(path: string, schema: z.ZodType<T>, init: RequestInit = {}): Promise<T> {
  const url = `${getApiBase()}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: apiHeaders(init.headers),
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

export function fetchOnboardingStatus(): Promise<OnboardingStatus> {
  return request('/api/onboarding/status', onboardingStatusSchema);
}

export function saveOnboarding(payload: OnboardingSavePayload): Promise<OnboardingStatus> {
  return request('/api/onboarding/save', onboardingStatusSchema, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
