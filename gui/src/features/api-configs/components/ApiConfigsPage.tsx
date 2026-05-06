import { useState } from 'react';

import { CredentialsCard } from '@/features/credentials';

import { useConfigs, useProfiles, useSaveConfigs } from '../hooks/useApiConfigs';
import type { ApiConfigEntry } from '../types';

import { ConfigEditor } from './ConfigEditor';
import { ProfileBar } from './ProfileBar';

/** API configs tab — Profile bar (cc-switch) + config list + editor modal. */
export function ApiConfigsPage(): JSX.Element {
  const configs = useConfigs();
  const profiles = useProfiles();
  const save = useSaveConfigs();
  const [editing, setEditing] = useState<{ config: ApiConfigEntry | null } | null>(null);

  const allConfigs = configs.data?.configs ?? [];
  const activeProfile = profiles.data?.active ?? null;
  const profileMembers = activeProfile
    ? new Set(profiles.data?.profiles[activeProfile] ?? [])
    : null;

  const onDelete = (target: ApiConfigEntry) => {
    if (!window.confirm(`删除 API 配置 "${target.name}"？`)) return;
    save.mutate(allConfigs.filter((c) => c.name !== target.name));
  };

  return (
    <div className="flex flex-col gap-4 p-4 max-w-3xl mx-auto">
      <header>
        <h1 className="text-xl font-semibold">API 配置</h1>
        <p className="text-sm text-muted-foreground">
          多渠道凭据 + Profile 切换（cc-switch 风格）。点 Profile 中"成员"来选哪些 configs 属于该 profile。
        </p>
      </header>

      <ProfileBar configNames={allConfigs.map((c) => c.name)} />

      {configs.isLoading ? <p className="text-muted-foreground">加载中…</p> : null}
      {configs.error ? (
        <p className="text-destructive text-sm">后端连接失败：{String(configs.error)}</p>
      ) : null}

      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium">凭据 ({allConfigs.length})</h2>
        <button
          type="button"
          onClick={() => setEditing({ config: null })}
          className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90"
        >
          新增配置
        </button>
      </div>

      {allConfigs.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          暂无凭据。点"新增配置"，或在终端 `python -m launcher.cli_init` 跑一次向导。
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {allConfigs.map((c) => {
            const inActiveProfile = profileMembers ? profileMembers.has(c.name) : null;
            return (
              <li
                key={c.name}
                className="rounded-lg border border-border bg-card p-3 flex items-center gap-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    {inActiveProfile === true ? (
                      <span title={`属于 profile ${activeProfile}`}>★</span>
                    ) : null}
                    <span className="font-medium truncate">{c.name}</span>
                    <span className="text-xs text-muted-foreground">[{c.kind}]</span>
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5 truncate">
                    {c.model ? `model: ${c.model}` : null}
                    {c.apibase ? `  ·  ${c.apibase}` : null}
                  </div>
                </div>
                <div className="flex gap-1 shrink-0">
                  <button
                    type="button"
                    onClick={() => setEditing({ config: c })}
                    className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    onClick={() => onDelete(c)}
                    className="px-2 py-0.5 text-xs rounded border border-destructive/40 text-destructive hover:bg-destructive/10"
                  >
                    删除
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {editing ? (
        <ConfigEditor
          config={editing.config}
          allConfigs={allConfigs}
          onClose={() => setEditing(null)}
        />
      ) : null}

      <hr className="border-border my-4" />
      <CredentialsCard />
    </div>
  );
}
