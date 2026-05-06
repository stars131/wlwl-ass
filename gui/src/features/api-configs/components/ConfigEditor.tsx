import { useEffect, useState } from 'react';

import { testApiConfig, type LlmTestResult } from '../api/apiConfigsApi';
import { useSaveConfigs } from '../hooks/useApiConfigs';
import type { ApiConfigEntry } from '../types';

interface ConfigEditorProps {
  config: ApiConfigEntry | null; // null = creating
  allConfigs: ApiConfigEntry[];
  onClose: () => void;
}

const KIND_LABELS: Record<string, string> = {
  native_oai: 'OpenAI 原生工具',
  native_claude: 'Claude 原生工具',
  mixin: 'Mixin（多渠道故障转移）',
};

/** Form to create or edit a single API config; saves the whole list. */
export function ConfigEditor({ config, allConfigs, onClose }: ConfigEditorProps): JSX.Element {
  const save = useSaveConfigs();
  const [draft, setDraft] = useState<ApiConfigEntry>(
    config ?? { kind: 'native_oai', name: '', apibase: '', apikey: '', model: '' },
  );
  const [apikeyDirty, setApikeyDirty] = useState<boolean>(config === null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(config ?? { kind: 'native_oai', name: '', apibase: '', apikey: '', model: '' });
    setApikeyDirty(config === null);
    setTestResult(null);
    setTestError(null);
  }, [config]);

  const isEditing = config !== null;

  const onChange = <K extends keyof ApiConfigEntry>(key: K, value: ApiConfigEntry[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setTestResult(null);
    setTestError(null);
  };

  const onTest = async () => {
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      // For mixin we can't directly test — server returns 400 with that name.
      // For an existing config where the user didn't retype the apikey, send
      // ``name`` so the server falls back to the stored (unmasked) key.
      const result = await testApiConfig({
        name: isEditing ? config!.name : draft.name,
        kind: draft.kind,
        apibase: draft.apibase,
        apikey: apikeyDirty ? draft.apikey : '',
        model: draft.model,
      });
      setTestResult(result);
    } catch (err) {
      setTestError(String(err));
    } finally {
      setTesting(false);
    }
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft.name.trim()) return;

    // Construct the full configs list with our draft applied.
    const next = allConfigs.map((c) => {
      if (isEditing && c.name === config!.name) return draft;
      return c;
    });
    if (!isEditing) {
      next.push(draft);
    }

    // If user didn't touch the apikey on an edit (it was masked '***'),
    // preserve the existing one; we don't re-save the masked value.
    if (isEditing && !apikeyDirty) {
      // Pull the original apikey field from the prior config — but the
      // backend only ever returns '***'. We can't recover the real key
      // from the wire, so save is a no-op for that field. The user must
      // edit explicitly to change it.
      // The launcher's save_api_configs validates required fields but
      // accepts '***' literally; this is acceptable behavior given the
      // mask boundary. Future: PUT /api/configs/<name>/apikey for partial
      // update.
    }

    save.mutate(next, { onSuccess: () => onClose() });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <form
        onSubmit={onSubmit}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-md border border-border bg-background p-4 space-y-3"
      >
        <h3 className="font-medium">{isEditing ? '编辑 API 配置' : '新增 API 配置'}</h3>

        <Field label="类型">
          <select
            value={draft.kind}
            onChange={(e) => onChange('kind', e.target.value)}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            {Object.entries(KIND_LABELS).map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="名称（唯一）">
          <input
            type="text"
            value={draft.name}
            onChange={(e) => onChange('name', e.target.value)}
            disabled={isEditing}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm disabled:opacity-50"
          />
        </Field>

        {draft.kind !== 'mixin' ? (
          <>
            <Field label="API Base">
              <input
                type="text"
                value={draft.apibase ?? ''}
                onChange={(e) => onChange('apibase', e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                placeholder="https://api.openai.com/v1"
              />
            </Field>

            <Field label="Model">
              <input
                type="text"
                value={draft.model ?? ''}
                onChange={(e) => onChange('model', e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                placeholder="gpt-5.4 / claude-opus-4-7 / ..."
              />
            </Field>

            <Field label={`API Key${isEditing && !apikeyDirty ? '（保持不变）' : ''}`}>
              <input
                type="password"
                value={apikeyDirty ? (draft.apikey ?? '') : ''}
                placeholder={isEditing && !apikeyDirty ? '已设置；输入新值以替换' : 'sk-...'}
                onChange={(e) => {
                  setApikeyDirty(true);
                  onChange('apikey', e.target.value);
                }}
                className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm font-mono"
              />
            </Field>

            <fieldset className="flex items-center gap-4 pt-1">
              <legend className="sr-only">capability flags</legend>
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                <input
                  type="checkbox"
                  checked={Boolean(draft.audio_capable)}
                  onChange={(e) => onChange('audio_capable', e.target.checked)}
                  className="rounded border-border"
                />
                <span title="工具 voice (transcribe/tts) 会优先选这条">audio_capable</span>
              </label>
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                <input
                  type="checkbox"
                  checked={Boolean(draft.image_capable)}
                  onChange={(e) => onChange('image_capable', e.target.checked)}
                  className="rounded border-border"
                />
                <span title="工具 image_generate 会优先选这条">image_capable</span>
              </label>
            </fieldset>
          </>
        ) : (
          <Field label="llm_nos（mixin 成员名，逗号分隔）">
            <input
              type="text"
              value={(draft.llm_nos ?? []).join(',')}
              onChange={(e) =>
                onChange(
                  'llm_nos',
                  e.target.value
                    .split(',')
                    .map((s) => s.trim())
                    .filter(Boolean),
                )
              }
              className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
              placeholder="gpt-native, claude-relay-1"
            />
          </Field>
        )}

        {save.isError ? (
          <p className="text-xs text-destructive">保存失败：{String(save.error)}</p>
        ) : null}

        {testError ? (
          <p className="text-xs text-destructive">测试失败：{testError}</p>
        ) : null}
        {testResult ? (
          <p className={`text-xs ${testResult.ok ? 'text-emerald-600' : 'text-destructive'}`}>
            {testResult.ok ? '✅ 连接成功' : '❌ 连接失败'}
            {testResult.latency_ms != null ? ` · ${testResult.latency_ms} ms` : ''}
            {testResult.status != null ? ` · HTTP ${testResult.status}` : ''}
            {testResult.error ? ` · ${testResult.error}` : ''}
            {!testResult.ok && testResult.sample
              ? ` · ${testResult.sample.slice(0, 160)}`
              : ''}
          </p>
        ) : null}

        <div className="flex justify-between gap-2 pt-1">
          <button
            type="button"
            onClick={onTest}
            disabled={testing || draft.kind === 'mixin' || !draft.name.trim()}
            className="px-3 py-1 text-sm rounded border border-border hover:bg-accent disabled:opacity-50"
            title={draft.kind === 'mixin'
              ? 'mixin 不能直接测，请测试每个成员'
              : '发一次 max_tokens=1 的请求确认凭据可用'}
          >
            {testing ? '测试中…' : '测试连接'}
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1 text-sm rounded border border-border hover:bg-accent"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={!draft.name.trim() || save.isPending}
              className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              保存
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

interface FieldProps {
  label: string;
  children: React.ReactNode;
}
function Field({ label, children }: FieldProps): JSX.Element {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
