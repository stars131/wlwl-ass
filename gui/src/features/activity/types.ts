/**
 * Activity log types — events emitted by WlwlAssHandler at each tool
 * dispatch boundary and at every turn end. Matches launcher/activity_log.py.
 */
import { z } from 'zod';

export const activityPhaseSchema = z.enum([
  'task_start',
  'task_end',
  'tool_start',
  'tool_end',
  'turn_end',
  'gui_step',
]);
export type ActivityPhase = z.infer<typeof activityPhaseSchema>;

export const activityEventSchema = z
  .object({
    ts: z.string(),
    pid: z.number().int().optional(),
    phase: activityPhaseSchema,
    turn: z.number().int().optional(),
    tool: z.string().optional(),
    args: z.record(z.unknown()).optional(),
    elapsed_s: z.number().optional(),
    summary: z.string().optional(),
    exit_reason: z.record(z.unknown()).optional(),
    status: z.string().optional(),
    action: z.string().optional(),
    target: z.string().optional(),
    screenshot_path: z.string().optional(),
    query_preview: z.string().optional(),
    outcome: z.string().optional(),
  })
  .passthrough();
export type ActivityEvent = z.infer<typeof activityEventSchema>;

export const activityResponseSchema = z.object({
  events: z.array(activityEventSchema),
  path: z.string(),
});
export type ActivityResponse = z.infer<typeof activityResponseSchema>;
