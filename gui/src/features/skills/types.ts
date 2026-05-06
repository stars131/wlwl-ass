/**
 * Skill catalogue types — SOP files under memory/ joined with runtime
 * outcome counts. Mirrors launcher/skills.py + activity_log.summarize_outcomes.
 */
import { z } from 'zod';

export const skillOutcomesSchema = z.object({
  ok: z.number().int().nonnegative(),
  max_turns: z.number().int().nonnegative(),
  exited: z.number().int().nonnegative(),
  other: z.number().int().nonnegative(),
  total: z.number().int().nonnegative(),
  last_seen: z.string(),
  success_rate: z.number().nullable(),
});
export type SkillOutcomes = z.infer<typeof skillOutcomesSchema>;

export const skillSchema = z.object({
  name: z.string(),
  title: z.string(),
  subtitle: z.string(),
  path: z.string(),
  size_bytes: z.number().int().nonnegative(),
  mtime: z.string(),
  outcomes: skillOutcomesSchema.nullable(),
});
export type Skill = z.infer<typeof skillSchema>;

export const skillsResponseSchema = z.object({
  skills: z.array(skillSchema),
});
export type SkillsResponse = z.infer<typeof skillsResponseSchema>;
