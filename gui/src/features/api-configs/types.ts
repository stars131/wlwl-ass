/**
 * Zod schemas + types for the API configs feature (cred CRUD + Profile).
 */
import { z } from 'zod';

export const configKindSchema = z.enum(['native_oai', 'native_claude', 'mixin']);
export type ConfigKind = z.infer<typeof configKindSchema>;

export const apiConfigCategories = ['language', 'multimodal', 'voice', 'utility'] as const;
export const apiConfigCategorySchema = z.enum(apiConfigCategories);
export type ApiConfigCategory = z.infer<typeof apiConfigCategorySchema>;

export const API_CONFIG_CATEGORY_LABELS: Record<ApiConfigCategory, string> = {
  language: '语言模型',
  multimodal: '多模态',
  voice: '语音',
  utility: '工具/其他',
};

export function getApiConfigCategory(value: unknown): ApiConfigCategory {
  const parsed = apiConfigCategorySchema.safeParse(value);
  return parsed.success ? parsed.data : 'language';
}

export const apiConfigEntrySchema = z.object({
  kind: z.string(),
  name: z.string(),
  category: apiConfigCategorySchema.optional(),
  priority: z.union([z.number(), z.string()]).optional(),
  apikey: z.string().optional(),
  apibase: z.string().optional(),
  model: z.string().optional(),
  api_mode: z.string().optional(),
  stream: z.boolean().optional(),
  max_tokens: z.union([z.number(), z.string()]).optional(),
  max_retries: z.union([z.number(), z.string()]).optional(),
  connect_timeout: z.union([z.number(), z.string()]).optional(),
  read_timeout: z.union([z.number(), z.string()]).optional(),
  temperature: z.union([z.number(), z.string()]).optional(),
  reasoning_effort: z.string().optional(),
  thinking_type: z.string().optional(),
  thinking_budget_tokens: z.union([z.number(), z.string()]).optional(),
  fake_cc_system_prompt: z.boolean().optional(),
  llm_nos: z.array(z.union([z.string(), z.number()])).optional(),
  var_name: z.string().optional(),
  // Capability opt-ins consumed by tools/voice_tools.py and
  // tools/image_generation.py to decide which config to route to.
  audio_capable: z.boolean().optional(),
  image_capable: z.boolean().optional(),
});
export type ApiConfigEntry = z.infer<typeof apiConfigEntrySchema>;

export const apiConfigsListSchema = z.object({ configs: z.array(apiConfigEntrySchema) });
export type ApiConfigsList = z.infer<typeof apiConfigsListSchema>;

export const profilesStateSchema = z.object({
  active: z.string().nullable(),
  profiles: z.record(z.string(), z.array(z.string())),
});
export type ProfilesState = z.infer<typeof profilesStateSchema>;
