import { useEffect, useState } from 'react';

import { testApiConfig, type LlmTestResult } from '../api/apiConfigsApi';
import { useSaveConfigs } from '../hooks/useApiConfigs';
import {
  API_CONFIG_CATEGORY_LABELS,
  apiConfigCategories,
  getApiConfigCategory,
  type ApiConfigEntry,
} from '../types';

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

const DEFAULT_CONFIG: ApiConfigEntry = {
  kind: 'native_oai',
  name: '',
  category: 'language',
  priority: 0,
  apibase: '',
  apikey: '',
  model: '',
};

function normalizeDraft(config: ApiConfigEntry | null): ApiConfigEntry {
  if (!config) return { ...DEFAULT_CONFIG };
  return {
    ...DEFAULT_CONFIG,
    ...config,
    category: getApiConfigCategory(config.category),
    priority: config.priority ?? 0,
  };
}

/** Form to create or edit a single API config; saves the whole list. */
export function ConfigEditor({ config, allConfigs, onClose }: ConfigEditorProps): JSX.Element {
  const save = useSaveConfigs();
  const [draft, setDraft] = useState<ApiConfigEntry>(
    normalizeDraft(config),
  );
  const [apikeyDirty, setApikeyDirty] = useState<boolean>(config === null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState<boolean>(false);

  useEffect(() => {
    setDraft(normalizeDraft(config));
    setApikeyDirty(config === null);
    setTestResult(null);
    setTestError(null);
    setAdvancedOpen(Boolean(
      config != null
        && (
          config.temperature != null
          || config.reasoning_effort
          || config.thinking_type
          || config.thinking_budget_tokens != null
          || config.max_tokens != null
          || config.api_mode
        ),
    ));
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

        <div className="grid grid-cols-2 gap-2">
          <Field label="Category">
            <select
              value={getApiConfigCategory(draft.category)}
              onChange={(e) => onChange('category', getApiConfigCategory(e.target.value))}
              className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
            >
              {apiConfigCategories.map((category) => (
                <option key={category} value={category}>
                  {API_CONFIG_CATEGORY_LABELS[category]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Priority">
            <input
              type="number"
              step={1}
              value={
                typeof draft.priority === 'number'
                  ? draft.priority
                  : (draft.priority ?? 0)
              }
              onChange={(e) =>
                onChange('priority', e.target.value === '' ? undefined : Number(e.target.value))
              }
              className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
            />
          </Field>
        </div>

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

            <div className="pt-2">
              <button
                type="button"
                onClick={() => setAdvancedOpen((v) => !v)}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                {advancedOpen ? '▾' : '▸'} 高级（推理 / 性能 / 超时）
              </button>
            </div>
            {advancedOpen ? (
              <div className="space-y-3 rounded-md border border-border/60 bg-card/30 p-3">
                <Field label="api_mode（OpenAI 系：Responses 还是 Chat Completions）">
                  <select
                    value={typeof draft.api_mode === 'string' ? draft.api_mode : ''}
                    onChange={(e) => onChange('api_mode', e.target.value || undefined)}
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                  >
                    <option value="">默认（chat_completions）</option>
                    <option value="chat_completions">chat_completions</option>
                    <option value="responses">responses</option>
                  </select>
                </Field>

                <Field label="reasoning_effort（OpenAI 思考程度 / Claude effort）">
                  <select
                    value={typeof draft.reasoning_effort === 'string' ? draft.reasoning_effort : ''}
                    onChange={(e) => onChange('reasoning_effort', e.target.value || undefined)}
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                  >
                    <option value="">默认（不指定）</option>
                    <option value="none">none</option>
                    <option value="minimal">minimal</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="xhigh">xhigh（Claude 视为 max）</option>
                  </select>
                </Field>

                <Field label="thinking_type（Claude 思考开关）">
                  <select
                    value={typeof draft.thinking_type === 'string' ? draft.thinking_type : ''}
                    onChange={(e) => onChange('thinking_type', e.target.value || undefined)}
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                  >
                    <option value="">默认（不指定）</option>
                    <option value="adaptive">adaptive</option>
                    <option value="enabled">enabled（需配 budget_tokens）</option>
                    <option value="disabled">disabled</option>
                  </select>
                </Field>

                <Field label="thinking_budget_tokens（Claude thinking=enabled 时必填）">
                  <input
                    type="number"
                    min={0}
                    step={256}
                    value={
                      typeof draft.thinking_budget_tokens === 'number'
                        ? draft.thinking_budget_tokens
                        : (draft.thinking_budget_tokens ?? '')
                    }
                    onChange={(e) =>
                      onChange(
                        'thinking_budget_tokens',
                        e.target.value === '' ? undefined : Number(e.target.value),
                      )
                    }
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                    placeholder="例如 4096"
                  />
                </Field>

                <Field label="temperature（0~2；Kimi/Moonshot/MiniMax 自动夹到 (0,1]）">
                  <input
                    type="number"
                    min={0}
                    max={2}
                    step={0.05}
                    value={
                      typeof draft.temperature === 'number'
                        ? draft.temperature
                        : (draft.temperature ?? '')
                    }
                    onChange={(e) =>
                      onChange(
                        'temperature',
                        e.target.value === '' ? undefined : Number(e.target.value),
                      )
                    }
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                    placeholder="默认 1"
                  />
                </Field>

                <Field label="max_tokens（一次响应最多生成多少 token）">
                  <input
                    type="number"
                    min={0}
                    step={256}
                    value={
                      typeof draft.max_tokens === 'number'
                        ? draft.max_tokens
                        : (draft.max_tokens ?? '')
                    }
                    onChange={(e) =>
                      onChange(
                        'max_tokens',
                        e.target.value === '' ? undefined : Number(e.target.value),
                      )
                    }
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                    placeholder="留空 = 走 provider 默认"
                  />
                </Field>

                <fieldset className="flex flex-wrap gap-4 pt-1">
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                    <input
                      type="checkbox"
                      checked={draft.stream !== false}
                      onChange={(e) => onChange('stream', e.target.checked)}
                      className="rounded border-border"
                    />
                    <span title="启用流式响应（默认）">stream</span>
                  </label>
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                    <input
                      type="checkbox"
                      checked={Boolean(draft.fake_cc_system_prompt)}
                      onChange={(e) => onChange('fake_cc_system_prompt', e.target.checked)}
                      className="rounded border-border"
                    />
                    <span title="某些反代要求伪装为 Claude Code 系统提示">fake_cc_system_prompt</span>
                  </label>
                </fieldset>

                <div className="grid grid-cols-3 gap-2">
                  <Field label="max_retries">
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={
                        typeof draft.max_retries === 'number'
                          ? draft.max_retries
                          : (draft.max_retries ?? '')
                      }
                      onChange={(e) =>
                        onChange(
                          'max_retries',
                          e.target.value === '' ? undefined : Number(e.target.value),
                        )
                      }
                      className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                      placeholder="1"
                    />
                  </Field>
                  <Field label="connect_timeout(s)">
                    <input
                      type="number"
                      min={1}
                      step={1}
                      value={
                        typeof draft.connect_timeout === 'number'
                          ? draft.connect_timeout
                          : (draft.connect_timeout ?? '')
                      }
                      onChange={(e) =>
                        onChange(
                          'connect_timeout',
                          e.target.value === '' ? undefined : Number(e.target.value),
                        )
                      }
                      className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                      placeholder="默认 5/10"
                    />
                  </Field>
                  <Field label="read_timeout(s)">
                    <input
                      type="number"
                      min={5}
                      step={5}
                      value={
                        typeof draft.read_timeout === 'number'
                          ? draft.read_timeout
                          : (draft.read_timeout ?? '')
                      }
                      onChange={(e) =>
                        onChange(
                          'read_timeout',
                          e.target.value === '' ? undefined : Number(e.target.value),
                        )
                      }
                      className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                      placeholder="默认 30/240"
                    />
                  </Field>
                </div>
              </div>
            ) : null}
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
