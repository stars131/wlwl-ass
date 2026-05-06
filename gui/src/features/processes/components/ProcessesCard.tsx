import { useState } from 'react';

import {
  useCleanupDeadProcesses,
  useKillProcess,
  useProcesses,
} from '../hooks/useProcesses';
import type { ProcessEntry } from '../api/processesApi';

/**
 * Process Registry card (#19).
 *
 * Surfaces every subprocess the launcher or the agent has registered:
 * `session:<id>` (streamlit children), `bot:<key>` (chat platform bots),
 * and any `process({"action":"register",...})` calls the agent itself
 * makes for headless browsers / shells / long tasks.
 *
 * Polling: 5s. Listing is cheap (one process_registry.list() call); the
 * `alive` flag is recomputed on every refresh, so killed processes flip
 * to grey within one cycle.
 *
 * Lives inside the Activity tab because both answer "what's running
 * right now". Sessions and Bots tabs only show their own subset; this
 * is the unified view.
 */
export function ProcessesCard(): JSX.Element {
  const processes = useProcesses();
  const kill = useKillProcess();
  const cleanup = useCleanupDeadProcesses();
  const [showDead, setShowDead] = useState(false);

  const rows = processes.data?.processes ?? [];
  const filtered = showDead ? rows : rows.filter((r) => r.alive);
  const deadCount = rows.length - rows.filter((r) => r.alive).length;

  const onKill = (entry: ProcessEntry, force: boolean) => {
    const label = `${entry.label} (pid=${entry.pid})`;
    if (!window.confirm(`${force ? '强制 kill' : '终止'} ${label}？`)) return;
    kill.mutate({ pid: entry.pid, force });
  };

  return (
    <section className="rounded-md border border-border bg-card p-3 flex flex-col gap-3">
      <header className="flex items-baseline justify-between gap-2 flex-wrap">
        <div>
          <h2 className="text-sm font-semibold">进程注册表</h2>
          <p className="text-xs text-muted-foreground">
            launcher 起的 streamlit / bot 子进程 + agent 通过 ``process`` 工具注册的后台任务。
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <label className="flex items-center gap-1 cursor-pointer text-muted-foreground">
            <input
              type="checkbox"
              checked={showDead}
              onChange={(e) => setShowDead(e.target.checked)}
              className="rounded border-border"
            />
            <span>显示已死 ({deadCount})</span>
          </label>
          <button
            type="button"
            onClick={() => cleanup.mutate()}
            disabled={cleanup.isPending || deadCount === 0}
            className="px-2 py-0.5 rounded border border-border hover:bg-accent disabled:opacity-50"
            title="把已经退出的注册项扫掉"
          >
            清理已死
          </button>
          <button
            type="button"
            onClick={() => processes.refetch()}
            disabled={processes.isFetching}
            className="px-2 py-0.5 rounded border border-border hover:bg-accent disabled:opacity-50"
          >
            刷新
          </button>
        </div>
      </header>

      {processes.isLoading ? (
        <p className="text-xs text-muted-foreground">加载中…</p>
      ) : null}
      {processes.error ? (
        <p className="text-xs text-destructive">加载失败：{String(processes.error)}</p>
      ) : null}

      {filtered.length === 0 && !processes.isLoading ? (
        <p className="text-xs text-muted-foreground italic">
          {rows.length === 0 ? '注册表为空。' : '没有存活进程（勾选"显示已死"看历史）。'}
        </p>
      ) : (
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="text-left border-b border-border text-muted-foreground">
              <th className="py-1.5 px-2">label</th>
              <th className="py-1.5 px-2">kind</th>
              <th className="py-1.5 px-2">pid</th>
              <th className="py-1.5 px-2">started_at</th>
              <th className="py-1.5 px-2 w-20">状态</th>
              <th className="py-1.5 px-2 text-right w-32">操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((entry) => (
              <tr key={`${entry.pid}-${entry.label}`} className="border-b border-border last:border-0">
                <td className="py-1.5 px-2 font-mono truncate max-w-[16rem]" title={entry.cmd}>
                  {entry.label}
                </td>
                <td className="py-1.5 px-2">{entry.kind}</td>
                <td className="py-1.5 px-2 tabular-nums">{entry.pid}</td>
                <td className="py-1.5 px-2 text-muted-foreground tabular-nums">
                  {entry.started_at}
                </td>
                <td className="py-1.5 px-2">
                  {entry.alive ? (
                    <span className="text-emerald-600 dark:text-emerald-400">🟢 alive</span>
                  ) : (
                    <span className="text-muted-foreground">⚪ dead</span>
                  )}
                </td>
                <td className="py-1.5 px-2 text-right">
                  {entry.alive ? (
                    <div className="inline-flex gap-1">
                      <button
                        type="button"
                        onClick={() => onKill(entry, false)}
                        disabled={kill.isPending}
                        className="px-2 py-0.5 rounded border border-border hover:bg-accent disabled:opacity-50"
                      >
                        终止
                      </button>
                      <button
                        type="button"
                        onClick={() => onKill(entry, true)}
                        disabled={kill.isPending}
                        className="px-2 py-0.5 rounded border border-destructive/40 text-destructive hover:bg-destructive/10 disabled:opacity-50"
                      >
                        强杀
                      </button>
                    </div>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {kill.isError ? (
        <p className="text-xs text-destructive">kill 失败：{String(kill.error)}</p>
      ) : null}
      {kill.data && !kill.data.ok ? (
        <p className="text-xs text-destructive">kill 失败：{kill.data.message}</p>
      ) : null}
      {cleanup.data ? (
        <p className="text-xs text-muted-foreground">已清理 {cleanup.data.removed} 个已死项。</p>
      ) : null}
    </section>
  );
}
