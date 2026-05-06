import { useEffect, useState } from 'react';

import { useCredentials, usePatchCredentials } from '../hooks/useCredentials';
import type { CredValue } from '../api/credentialsApi';

const BOT_LABELS: Record<string, string> = {
  tg: 'Telegram',
  qq: 'QQ',
  feishu: '飞书',
  wecom: '企业微信',
  dingtalk: '钉钉',
};

const SECRET_HINT = (field: string) =>
  field.includes('secret') || field.includes('token') ? '***' : '';

const isListField = (field: string) => field.endsWith('_allowed_users');

/**
 * Bot credential editor card. Renders one collapsible block per bot with
 * its whitelisted fields. Saves only the diff so unmasked secrets survive
 * round-trips.
 */
export function CredentialsCard(): JSX.Element {
  const creds = useCredentials();
  const patch = usePatchCredentials();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState<Record<string, boolean>>({});
  const [open, setOpen] = useState<string | null>(null);

  // Whenever server state lands, copy values into local string draft (lists
  // are joined by comma for the textbox).
  useEffect(() => {
    if (!creds.data) return;
    const next: Record<string, string> = {};
    for (const bot of Object.keys(creds.data.fields)) {
      for (const field of creds.data.fields[bot] ?? []) {
        const v = creds.data.values[bot]?.[field];
        if (Array.isArray(v)) next[field] = v.join(', ');
        else next[field] = String(v ?? '');
      }
    }
    setDraft(next);
    setDirty({});
  }, [creds.data]);

  const onChange = (field: string, value: string) => {
    setDraft((d) => ({ ...d, [field]: value }));
    setDirty((d) => ({ ...d, [field]: true }));
  };

  const onSave = (bot: string) => {
    const fields = creds.data?.fields[bot] ?? [];
    const payload: Record<string, CredValue> = {};
    for (const field of fields) {
      if (!dirty[field]) continue;
      const value = draft[field] ?? '';
      if (isListField(field)) {
        payload[field] = value
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
      } else {
        payload[field] = value;
      }
    }
    if (Object.keys(payload).length === 0) return;
    patch.mutate(payload);
  };

  if (creds.isLoading) {
    return <p className="text-sm text-muted-foreground">加载凭据中…</p>;
  }
  if (creds.error || !creds.data) {
    return <p className="text-sm text-destructive">加载凭据失败：{String(creds.error)}</p>;
  }

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-sm font-medium">Bot 凭据</h2>
        <p className="text-xs text-muted-foreground">
          直接写入 ~/.wlwl-ass/config.json（POSIX 下自动 chmod 600）。带 *** 的字段保持原值不动；输入新值才替换。
        </p>
      </div>

      {Object.keys(creds.data.fields)
        .filter((b) => b in BOT_LABELS)
        .map((bot) => {
          const fields = creds.data!.fields[bot] ?? [];
          const isOpen = open === bot;
          const hasAny = fields.some(
            (f) => (creds.data!.values[bot]?.[f] ?? '') !== '' && creds.data!.values[bot]?.[f] !== undefined,
          );
          return (
            <div key={bot} className="rounded-md border border-border bg-card">
              <button
                type="button"
                onClick={() => setOpen(isOpen ? null : bot)}
                className="w-full flex items-center justify-between px-3 py-2 text-sm hover:bg-accent rounded-md"
              >
                <span>
                  <span className="font-medium">{BOT_LABELS[bot]}</span>
                  <span className="text-xs text-muted-foreground ml-2">
                    {hasAny ? '✅ 已配置' : '❌ 未配置'}
                  </span>
                </span>
                <span>{isOpen ? '▾' : '▸'}</span>
              </button>
              {isOpen ? (
                <div className="px-3 pb-3 space-y-2">
                  {fields.map((field) => (
                    <label key={field} className="flex flex-col gap-1 text-sm">
                      <span className="text-xs text-muted-foreground">{field}</span>
                      <input
                        type={
                          field.includes('secret') || field.includes('token')
                            ? 'password'
                            : 'text'
                        }
                        value={draft[field] ?? ''}
                        onChange={(e) => onChange(field, e.target.value)}
                        placeholder={SECRET_HINT(field)}
                        className="rounded-md border border-border bg-background px-2 py-1 font-mono"
                      />
                      {isListField(field) ? (
                        <span className="text-xs text-muted-foreground">
                          逗号分隔，如 <code>ou_a, ou_b</code>。`*` 表示公开访问（仅部分平台支持）。
                        </span>
                      ) : null}
                    </label>
                  ))}
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={() => onSave(bot)}
                      disabled={patch.isPending}
                      className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                    >
                      保存 {BOT_LABELS[bot]} 凭据
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          );
        })}

      {patch.isError ? (
        <p className="text-xs text-destructive">保存失败：{String(patch.error)}</p>
      ) : null}
      {patch.isSuccess ? <p className="text-xs text-muted-foreground">已保存</p> : null}
    </div>
  );
}
