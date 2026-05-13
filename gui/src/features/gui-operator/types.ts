import { z } from 'zod';

export const guiStepSchema = z
  .object({
    step: z.number().int().optional(),
    status: z.string().optional(),
    action_text: z.string().optional(),
    action: z.string().optional(),
    error: z.string().optional(),
    detail: z.string().optional(),
    prediction: z.string().optional(),
    paused_s: z.number().optional(),
    elapsed_s: z.number().optional(),
    observe: z.record(z.unknown()).optional(),
    parsed: z.record(z.unknown()).optional(),
    result: z.record(z.unknown()).optional(),
  })
  .passthrough();
export type GuiStep = z.infer<typeof guiStepSchema>;

export const guiRunSchema = z.object({
  run_id: z.string(),
  instruction: z.string(),
  max_loop: z.number(),
  loop_wait: z.number(),
  dry_run: z.boolean(),
  backend: z.string(),
  all_screens: z.boolean(),
  include_base64: z.boolean(),
  status: z.string(),
  run_dir: z.string().default(''),
  error: z.string().default(''),
  created_at: z.number(),
  updated_at: z.number(),
  steps: z.array(guiStepSchema),
  result: z.record(z.unknown()).nullable().optional(),
});
export type GuiRun = z.infer<typeof guiRunSchema>;

export const guiRunsResponseSchema = z.object({
  runs: z.array(guiRunSchema),
});
export type GuiRunsResponse = z.infer<typeof guiRunsResponseSchema>;

export const guiRunResponseSchema = z.object({
  run: guiRunSchema,
});
export type GuiRunResponse = z.infer<typeof guiRunResponseSchema>;

export const guiSidecarStatusSchema = z.object({
  configured: z.boolean(),
  url: z.string(),
  node_available: z.boolean(),
  sidecar_package: z.string(),
  mode: z.string(),
  message: z.string(),
});
export type GuiSidecarStatus = z.infer<typeof guiSidecarStatusSchema>;

export interface StartGuiRunInput {
  instruction: string;
  max_loop: number;
  loop_wait: number;
  dry_run: boolean;
  backend: string;
  all_screens: boolean;
  include_base64: boolean;
}
