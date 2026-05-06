/**
 * First-run wizard types — mirrors launcher/onboarding.py.
 */
import { z } from 'zod';

export const onboardingStatusSchema = z.object({
  needs_setup: z.boolean(),
  reason: z.string(),
  env_recognized: z.boolean(),
  has_mykey: z.boolean(),
  providers: z.array(z.string()),
});
export type OnboardingStatus = z.infer<typeof onboardingStatusSchema>;

export const onboardingSavePayloadSchema = z.object({
  provider: z.string(),
  apikey: z.string(),
  base_url: z.string().optional().default(''),
  model: z.string().optional().default(''),
});
export type OnboardingSavePayload = z.infer<typeof onboardingSavePayloadSchema>;
