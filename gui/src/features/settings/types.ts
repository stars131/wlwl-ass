/**
 * Settings types (mirrors launcher/launch_config.py DEFAULT_OPTIONS).
 *
 * Schema is permissive on extra keys so future Python-side additions don't
 * break the GUI immediately — we just expose the fields we render.
 */
import { z } from 'zod';

export const settingsSchema = z.object({
  tg: z.boolean(),
  qq: z.boolean(),
  feishu: z.boolean(),
  wecom: z.boolean(),
  dingtalk: z.boolean(),
  wechat: z.boolean(),
  scheduler: z.boolean(),
  llm_no: z.number().int(),
  permission_mode: z.string(),
  project_root: z.string(),
  use_project_context: z.boolean(),
  autonomous_enabled: z.boolean(),
});
export type Settings = z.infer<typeof settingsSchema>;

export const settingsResponseSchema = z.object({ settings: settingsSchema });

export const PERMISSION_MODES = ['auto', 'ask', 'read-only', 'dangerous'] as const;
export type PermissionMode = (typeof PERMISSION_MODES)[number];
