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

export const requestModeSchema = z.enum(['auto', 'chat', 'task', 'canvas']);
export type RequestMode = z.infer<typeof requestModeSchema>;

export const assistantModeSchema = z.enum(['chat', 'task', 'canvas', 'task_canvas']);
export type AssistantMode = z.infer<typeof assistantModeSchema>;

export const chatArtifactSchema = z.object({
  id: z.string(),
  title: z.string(),
  kind: z.string().default('markdown'),
  source: z.string().optional(),
  content: z.string().optional().default(''),
  path: z.string().optional(),
  artifact_path: z.string().optional(),
  created_at: z.string().optional(),
});
export type ChatArtifact = z.infer<typeof chatArtifactSchema>;

export const taskStepSchema = z.object({
  id: z.string(),
  label: z.string(),
  status: z.enum(['pending', 'running', 'done', 'error', 'aborted']).default('pending'),
});
export type TaskStep = z.infer<typeof taskStepSchema>;

export const taskRunSchema = z.object({
  steps: z.array(taskStepSchema).default([]),
});
export type TaskRun = z.infer<typeof taskRunSchema>;

export const intentInfoSchema = z.object({
  requested_mode: requestModeSchema.default('auto'),
  mode: assistantModeSchema.default('chat'),
  intent: z.string().default('conversation'),
  confidence: z.number().optional(),
  signals: z.record(z.boolean()).optional(),
});
export type IntentInfo = z.infer<typeof intentInfoSchema>;

export const chatMessageSchema = z.object({
  id: z.string(),
  seq: z.number().int(),
  role: z.enum(['user', 'assistant', 'system']),
  content: z.string(),
  status: z.enum(['running', 'done', 'error', 'aborted']).default('done'),
  created_at: z.string(),
  updated_at: z.string().optional(),
  requested_mode: requestModeSchema.optional(),
  mode: assistantModeSchema.optional(),
  intent: intentInfoSchema.optional(),
  task: taskRunSchema.nullable().optional(),
  artifacts: z.array(chatArtifactSchema).optional().default([]),
  debug_content: z.string().optional().default(''),
});
export type ChatMessage = z.infer<typeof chatMessageSchema>;

export const chatMessagesSchema = z.object({
  messages: z.array(chatMessageSchema),
  running: z.boolean().optional(),
});
export type ChatMessages = z.infer<typeof chatMessagesSchema>;

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
