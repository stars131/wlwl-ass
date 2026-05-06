/**
 * Diagnostic types — mirrors launcher/doctor.py::run_diagnostics.
 */
import { z } from 'zod';

export const checkSeveritySchema = z.enum(['ok', 'warn', 'fail', 'info']);
export type CheckSeverity = z.infer<typeof checkSeveritySchema>;

export const checkSchema = z.object({
  id: z.string(),
  title: z.string(),
  severity: checkSeveritySchema,
  detail: z.string().optional().default(''),
  fix: z.string().optional().default(''),
});
export type DoctorCheck = z.infer<typeof checkSchema>;

export const doctorReportSchema = z.object({
  project_root: z.string(),
  summary: z.object({
    ok: z.number().int().nonnegative().optional().default(0),
    warn: z.number().int().nonnegative().optional().default(0),
    fail: z.number().int().nonnegative().optional().default(0),
    info: z.number().int().nonnegative().optional().default(0),
  }),
  checks: z.array(checkSchema),
});
export type DoctorReport = z.infer<typeof doctorReportSchema>;
