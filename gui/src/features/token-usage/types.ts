/**
 * Token usage counters surfaced from llmcore. Mirrors
 * ``llmcore.get_token_usage()`` shape.
 */
import { z } from 'zod';

export const tokenTotalsSchema = z.object({
  input: z.number().int().nonnegative(),
  output: z.number().int().nonnegative(),
  cache_creation: z.number().int().nonnegative(),
  cache_read: z.number().int().nonnegative(),
  calls: z.number().int().nonnegative(),
});
export type TokenTotals = z.infer<typeof tokenTotalsSchema>;

export const tokenUsageSchema = z.object({
  totals: tokenTotalsSchema,
  by_mode: z.record(tokenTotalsSchema),
  since: z.string(),
  last_ts: z.string(),
  cache_hit_rate: z.number().nullable(),
  recent: z.array(
    z.object({
      ts: z.string(),
      api_mode: z.string(),
      input: z.number().int().nonnegative(),
      output: z.number().int().nonnegative(),
      cache_creation: z.number().int().nonnegative(),
      cache_read: z.number().int().nonnegative(),
    }),
  ),
});
export type TokenUsage = z.infer<typeof tokenUsageSchema>;
