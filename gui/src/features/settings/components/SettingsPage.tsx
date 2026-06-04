import { useEffect, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { DoctorPanel } from '@/features/doctor';
import { PlaybookCard } from '@/features/playbook';

import { useSettings, usePatchSettings } from '../hooks/useSettings';
import { PERMISSION_MODES, type Settings } from '../types';
import { RadarCard } from './RadarCard';

/**
 * Settings tab — global launcher options. Local form state mirrors the
 * server snapshot; "保存" submits the diff via PUT /api/settings (which
 * the backend merges into launcher_options.json).
 */
export function SettingsPage(): JSX.Element {
  const { t } = useTranslation();
  const settings = useSettings();
  const patch = usePatchSettings();

  const [draft, setDraft] = useState<Settings | null>(null);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (settings.data && !dirty) setDraft(settings.data);
  }, [settings.data, dirty]);

  if (settings.isLoading) {
    return <p className="p-4 text-muted-foreground">{t('common.loading')}</p>;
  }
  if (settings.error || !draft) {
    return (
      <p className="p-4 text-destructive">
        {t('common.backendLoadFailed', { error: String(settings.error) })}
      </p>
    );
  }

  const update = <K extends keyof Settings>(k: K, v: Settings[K]) => {
    setDraft((d) => (d === null ? d : { ...d, [k]: v }));
    setDirty(true);
  };

  const onSave = (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    patch.mutate(draft, {
      onSuccess: (next) => {
        setDraft(next);
        setDirty(false);
      },
    });
  };

  const onReset = () => {
    if (settings.data) {
      setDraft(settings.data);
      setDirty(false);
    }
  };

  return (
    <form onSubmit={onSave} className="flex flex-col gap-6 p-4 max-w-2xl mx-auto">
      <header>
        <h1 className="text-xl font-semibold">{t('settings.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('settings.subtitle')}</p>
      </header>

      <Group title="会话默认">
        <Field label="默认 LLM 索引（llm_no）">
          <input
            type="number"
            min={0}
            value={draft.llm_no}
            onChange={(e) => update('llm_no', Math.max(0, Number(e.target.value) || 0))}
            className="w-32 rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
          <span className="ml-2 text-xs text-muted-foreground">
            session 也可以在「会话」tab 用 API 选择器单独覆盖（按名字）。
          </span>
        </Field>

        <Field label="权限模式（permission_mode）">
          <select
            value={draft.permission_mode}
            onChange={(e) => update('permission_mode', e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            {PERMISSION_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </Field>

        <Field label="项目根（project_root）">
          <input
            type="text"
            value={draft.project_root}
            onChange={(e) => update('project_root', e.target.value)}
            placeholder="留空 = 不指定"
            className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>

        <Toggle
          checked={draft.use_project_context}
          onChange={(v) => update('use_project_context', v)}
          label="注入项目上下文（推荐）"
        />
        <Toggle
          checked={draft.autonomous_enabled}
          onChange={(v) => update('autonomous_enabled', v)}
          label="自主流程（autonomous_enabled）"
        />
      </Group>

      <Group title="后台进程">
        <Toggle
          checked={draft.scheduler}
          onChange={(v) => update('scheduler', v)}
          label="启用 L4 任务调度器"
        />
      </Group>

      <RadarCard />

      <PlaybookCard />

      <div className="flex items-center justify-end gap-2 sticky bottom-0 bg-background py-2 border-t border-border">
        {patch.isError ? (
          <span className="text-xs text-destructive mr-auto">
            {t('common.saveFailed', { error: String(patch.error) })}
          </span>
        ) : null}
        {patch.isSuccess ? (
          <span className="text-xs text-muted-foreground mr-auto">{t('common.saved')}</span>
        ) : null}
        {dirty && !patch.isPending ? (
          <span className="text-xs text-muted-foreground mr-auto">有未保存更改</span>
        ) : null}
        <button
          type="button"
          onClick={onReset}
          className="px-3 py-1 text-sm rounded border border-border hover:bg-accent"
        >
          {t('common.reset')}
        </button>
        <button
          type="submit"
          disabled={patch.isPending}
          className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {t('common.save')}
        </button>
      </div>
      <DoctorPanel />
    </form>
  );
}

interface GroupProps {
  title: string;
  children: React.ReactNode;
}
function Group({ title, children }: GroupProps): JSX.Element {
  return (
    <fieldset className="rounded-md border border-border p-3">
      <legend className="px-1 text-sm font-medium">{title}</legend>
      <div className="flex flex-col gap-3 pt-1">{children}</div>
    </fieldset>
  );
}

interface FieldProps {
  label: string;
  children: React.ReactNode;
}
function Field({ label, children }: FieldProps): JSX.Element {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground text-xs">{label}</span>
      <div className="flex items-center">{children}</div>
    </label>
  );
}

interface ToggleProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}
function Toggle({ checked, onChange, label }: ToggleProps): JSX.Element {
  return (
    <label className="flex items-center gap-2 text-sm cursor-pointer">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}
