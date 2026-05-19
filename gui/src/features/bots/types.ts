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
  // PID owning the singleton lock port when running_external=true. Non-null
  // means BotManager has *adopted* the orphan and can stop/restart it via PID
  // even without holding the Popen handle.
  lock_holder_pid: z.number().nullable().optional(),
  running: z.boolean(),
  auto_start: z.boolean().default(true),
  log_path: z.string(),
  // Per-bot LLM binding (added 2026-05-19). Empty string = default behaviour.
  // Format: "config:<name>" or "profile:<name>". Currently honored only by
  // the two Feishu frontends (see _LLM_BINDING_SUPPORTED_BOTS in api_server).
  llm_binding: z.string().default(''),
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

// LLM binding picker source. configs = single-pin candidates,
// profiles = fallback-chain candidates. Mirrors
// launcher/api_server.py::_route_bot_llm_options.
export const llmOptionsSchema = z.object({
  configs: z.array(z.object({
    name: z.string(),
    var_name: z.string().default(''),
    kind: z.string().default(''),
  })),
  profiles: z.array(z.object({
    name: z.string(),
    members: z.array(z.string()).default([]),
  })),
});
export type LlmOptions = z.infer<typeof llmOptionsSchema>;

export const botBindingResponseSchema = z.object({
  key: z.string(),
  binding: z.string(),
  restart_required: z.boolean(),
});
export type BotBindingResponse = z.infer<typeof botBindingResponseSchema>;
