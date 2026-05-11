/**
 * Zod schemas + types for the Bots feature.
 *
 * Mirrors `/api/bots` shape from launcher/api_server.py::_route_bots_list.
 */
import { z } from 'zod';

export const botRowSchema = z.object({
  key: z.string(),
  display_name: z.string(),
  script: z.string(),
  configured: z.boolean(),
  missing_fields: z.array(z.string()).default([]),
  sdk_installed: z.boolean(),
  missing_modules: z.array(z.string()).default([]),
  running_self: z.boolean(),
  running_external: z.boolean(),
  running: z.boolean(),
  auto_start: z.boolean().default(true),
  log_path: z.string(),
});
export type BotRow = z.infer<typeof botRowSchema>;

export const botsListSchema = z.object({ bots: z.array(botRowSchema) });
export type BotsList = z.infer<typeof botsListSchema>;

export const botActionResponseSchema = z.object({
  key: z.string(),
  message: z.string(),
});
export type BotActionResponse = z.infer<typeof botActionResponseSchema>;

export const botLogSchema = z.object({
  key: z.string(),
  path: z.string(),
  lines: z.array(z.string()),
  exists: z.boolean(),
});
export type BotLog = z.infer<typeof botLogSchema>;
