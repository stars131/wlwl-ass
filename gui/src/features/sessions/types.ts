/**
 * Zod schemas + inferred types for the Sessions feature.
 *
 * Mirrors the JSON shapes produced by `launcher/api_server.py`:
 *   - `Project` from /api/projects (list) and /api/projects/{id}
 *   - `ApiConfig` from /api/configs (apikey is masked '***' for read paths)
 *   - `ProfilesState` from /api/profiles
 */
import { z } from 'zod';

export const projectSchema = z.object({
  id: z.string(),
  name: z.string(),
  port: z.number().int().nullable().optional(),
  pid: z.number().int().nullable().optional(),
  pinned: z.boolean(),
  description: z.string().default(''),
  created_at: z.string().optional(),
  last_active: z.string().optional(),
  updated_at: z.string().optional(),
  last_error: z.string().optional(),
  llm_no: z.number().int(),
  llm_config_name: z.string().default(''),
  permission_mode: z.string().optional(),
  project_root: z.string().optional(),
  use_project_context: z.boolean().optional(),
  autonomous_enabled: z.boolean().optional(),
  log_path: z.string().optional(),
  running: z.boolean().optional(),
});
export type Project = z.infer<typeof projectSchema>;

export const projectsListSchema = z.object({
  projects: z.array(projectSchema),
  active_id: z.string().nullable(),
});
export type ProjectsList = z.infer<typeof projectsListSchema>;

export const apiConfigSchema = z.object({
  kind: z.string(),
  name: z.string(),
  apikey: z.string().optional(),
  apibase: z.string().optional(),
  model: z.string().optional(),
  var_name: z.string().optional(),
  llm_nos: z.array(z.union([z.string(), z.number()])).optional(),
});
export type ApiConfig = z.infer<typeof apiConfigSchema>;

export const configsListSchema = z.object({ configs: z.array(apiConfigSchema) });
export type ConfigsList = z.infer<typeof configsListSchema>;

export const profilesStateSchema = z.object({
  active: z.string().nullable(),
  profiles: z.record(z.string(), z.array(z.string())),
});
export type ProfilesState = z.infer<typeof profilesStateSchema>;
