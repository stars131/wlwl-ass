import { z } from 'zod';

import { ApiError } from '@/lib/api';
import { getApiBase } from '@/lib/env';

import { doctorReportSchema, type DoctorReport } from '../types';

async function request<S extends z.ZodTypeAny>(path: string, schema: S): Promise<z.output<S>> {
  const url = `${getApiBase()}${path}`;
  const res = await fetch(url, { headers: { 'content-type': 'application/json' } });
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

export function fetchDoctor(includeNetwork: boolean = false): Promise<DoctorReport> {
  const qs = includeNetwork ? '?network=1' : '';
  return request(`/api/doctor${qs}`, doctorReportSchema);
}
