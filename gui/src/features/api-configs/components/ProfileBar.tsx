import { useState } from 'react';

import {
  useDeleteProfile,
  useProfiles,
  useRenameProfile,
  useSetActiveProfile,
  useUpsertProfile,
} from '../hooks/useApiConfigs';

interface ProfileBarProps {
  configNames: string[];
}

/** Top-of-tab profile selector (cc-switch UX). */
export function ProfileBar({ configNames }: ProfileBarProps): JSX.Element {
  const profiles = useProfiles();
  const setActive = useSetActiveProfile();
  const upsert = useUpsertProfile();
  const renameMut = useRenameProfile();
  const del = useDeleteProfile();

  const [editingMembers, setEditingMembers] = useState<string | null>(null);

  const active = profiles.data?.active ?? null;
  const allProfiles = Object.entries(profiles.data?.profiles ?? {}).sort(([a], [b]) =>
    a.localeCompare(b),
  );

  const onCreate = () => {
    const name = window.prompt('新建 profile 名称：');
    if (!name?.trim()) return;
    upsert.mutate({ name: name.trim(), members: [] }, {
      onSuccess: () => {
        setActive.mutate(name.trim());
        setEditingMembers(name.trim());
      },
    });
  };

  const onRename = () => {
    if (!active) return;
    const newName = window.prompt('改名为：', active);
    if (!newName?.trim() || newName === active) return;
    renameMut.mutate({ oldName: active, newName: newName.trim() });
  };

  const onDelete = () => {
    if (!active) return;
    if (!window.confirm(`删除 profile "${active}"？仅删除分组定义，不影响 configs。`)) return;
    del.mutate(active);
  };

  return (
    <>
      <div className="flex items-center gap-2 flex-wrap rounded-md border border-border bg-card p-2">
        <span className="text-sm font-medium">Profile</span>
        <select
          value={active ?? ''}
          onChange={(e) => setActive.mutate(e.target.value || null)}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm flex-1 min-w-[10rem]"
        >
          <option value="">(无 profile, 全部启用)</option>
          {allProfiles.map(([name]) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={onCreate}
          className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
        >
          新建
        </button>
        <button
          type="button"
          onClick={onRename}
          disabled={!active}
          className="px-2 py-1 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
        >
          改名
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={!active}
          className="px-2 py-1 text-xs rounded border border-destructive/50 text-destructive hover:bg-destructive/10 disabled:opacity-50"
        >
          删除
        </button>
        <button
          type="button"
          onClick={() => setEditingMembers(active)}
          disabled={!active}
          className="px-2 py-1 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
        >
          成员
        </button>
      </div>

      {editingMembers ? (
        <ProfileMembersDialog
          profileName={editingMembers}
          configNames={configNames}
          initialMembers={profiles.data?.profiles[editingMembers] ?? []}
          onClose={() => setEditingMembers(null)}
          onSave={(members) => {
            upsert.mutate(
              { name: editingMembers, members },
              { onSuccess: () => setEditingMembers(null) },
            );
          }}
        />
      ) : null}
    </>
  );
}

interface ProfileMembersDialogProps {
  profileName: string;
  configNames: string[];
  initialMembers: string[];
  onClose: () => void;
  onSave: (members: string[]) => void;
}

function ProfileMembersDialog({
  profileName,
  configNames,
  initialMembers,
  onClose,
  onSave,
}: ProfileMembersDialogProps): JSX.Element {
  const [picked, setPicked] = useState<Set<string>>(new Set(initialMembers));
  const toggle = (name: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-md border border-border bg-background p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="font-medium mb-1">编辑 profile 成员: {profileName}</h3>
        <p className="text-xs text-muted-foreground mb-3">勾选属于此 profile 的 configs。</p>
        {configNames.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无 API 配置可加入。</p>
        ) : (
          <div className="space-y-1 max-h-96 overflow-auto">
            {configNames.map((name) => (
              <label key={name} className="flex items-center gap-2 text-sm cursor-pointer hover:bg-accent rounded px-1.5 py-1">
                <input
                  type="checkbox"
                  checked={picked.has(name)}
                  onChange={() => toggle(name)}
                />
                <span>{name}</span>
              </label>
            ))}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1 text-sm rounded border border-border hover:bg-accent"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => onSave(Array.from(picked))}
            className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90"
          >
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
