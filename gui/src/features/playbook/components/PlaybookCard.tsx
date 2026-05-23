import { useMemo, useState } from 'react';

import type { PlaybookEntry } from '../api/playbookApi';
import { useDecidePlaybookEntry, usePlaybook } from '../hooks/usePlaybook';

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

export function PlaybookCard(): JSX.Element {
  const playbook = usePlaybook();
  const decide = useDecidePlaybookEntry();
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const entries = playbook.data?.entries ?? [];
  const pending = useMemo(
    () => entries.filter((entry) => entry.status === 'pending'),
    [entries],
  );
  const active = useMemo(
    () => entries.filter((entry) => entry.status === 'active').slice(0, 8),
    [entries],
  );
  const rejected = playbook.data?.stats.status.rejected ?? 0;

  const handleDecide = (entry: PlaybookEntry, decision: 'accept' | 'reject') => {
    if (
      decision === 'accept' &&
      !window.confirm('采纳后，这条 Playbook 会注入未来系统提示。确认采纳？')
    ) {
      return;
    }
    decide.mutate({
      id: entry.id,
      decision,
      note: decision === 'accept' ? 'accepted from GUI' : 'rejected from GUI',
    });
  };

  return (
    <section className="rounded-md border border-border p-3">
      <header className="flex items-baseline justify-between gap-2 flex-wrap">
        <div>
          <h2 className="text-sm font-semibold">Reviewed Playbook</h2>
          <p className="text-xs text-muted-foreground">
            可复用执行经验的审核队列；只有采纳后的条目会进入未来系统提示。
          </p>
        </div>
        <span className="text-xs text-muted-foreground">
          {pending.length} 待审 · {active.length} 生效 · {rejected} 拒绝
        </span>
      </header>

      <div className="mt-3 flex flex-col gap-2">
        {playbook.isLoading ? (
          <p className="text-xs text-muted-foreground">加载中...</p>
        ) : null}
        {playbook.error ? (
          <p className="text-xs text-destructive">加载失败：{String(playbook.error)}</p>
        ) : null}

        {pending.length === 0 && !playbook.isLoading ? (
          <p className="text-xs text-muted-foreground italic">没有待审 Playbook。</p>
        ) : null}

        <ul className="flex flex-col gap-2">
          {pending.map((entry) => (
            <PlaybookRow
              key={entry.id}
              entry={entry}
              expanded={expandedId === entry.id}
              onToggle={() => setExpandedId((cur) => (cur === entry.id ? null : entry.id))}
              onDecide={handleDecide}
              isPending={decide.isPending}
            />
          ))}
        </ul>

        {active.length > 0 ? (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">
              已生效条目（{active.length}）
            </summary>
            <ul className="mt-2 flex flex-col gap-1">
              {active.map((entry) => (
                <li
                  key={entry.id}
                  className="rounded border border-border bg-muted/40 px-2 py-1"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-emerald-600 dark:text-emerald-400">active</span>
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {entry.category}
                    </span>
                    <span className="ml-auto text-[11px] text-muted-foreground">
                      {formatDateTime(entry.decided_at)}
                    </span>
                  </div>
                  <p className="mt-1 text-[11px]">{entry.content}</p>
                </li>
              ))}
            </ul>
          </details>
        ) : null}

        {decide.isError ? (
          <p className="text-xs text-destructive">操作失败：{String(decide.error)}</p>
        ) : null}
        {decide.data && decide.data.ok === false ? (
          <p className="text-xs text-destructive">操作失败：{decide.data.message}</p>
        ) : null}
      </div>
    </section>
  );
}

interface RowProps {
  entry: PlaybookEntry;
  expanded: boolean;
  onToggle: () => void;
  onDecide: (entry: PlaybookEntry, decision: 'accept' | 'reject') => void;
  isPending: boolean;
}

function PlaybookRow({
  entry,
  expanded,
  onToggle,
  onDecide,
  isPending,
}: RowProps): JSX.Element {
  return (
    <li className="rounded border border-border bg-background p-2 flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <div className="min-w-0">
          <span className="font-mono text-xs">{entry.category}</span>
          <span className="ml-2 text-[11px] text-muted-foreground">
            {formatDateTime(entry.created_at)}
          </span>
        </div>
        <div className="flex gap-1 shrink-0">
          <button
            type="button"
            onClick={onToggle}
            className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
          >
            {expanded ? '收起' : '详情'}
          </button>
          <button
            type="button"
            onClick={() => onDecide(entry, 'accept')}
            disabled={isPending}
            className="px-2 py-0.5 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            采纳
          </button>
          <button
            type="button"
            onClick={() => onDecide(entry, 'reject')}
            disabled={isPending}
            className="px-2 py-0.5 text-xs rounded border border-destructive/40 text-destructive hover:bg-destructive/10 disabled:opacity-50"
          >
            拒绝
          </button>
        </div>
      </div>

      <p className="text-xs leading-relaxed">{entry.content}</p>

      {expanded ? (
        <div className="rounded border border-border bg-muted/30 p-2 text-[11px] text-muted-foreground">
          {entry.rationale ? <p>理由：{entry.rationale}</p> : null}
          {entry.source ? (
            <p>
              来源：{entry.source}
              {entry.source_turn ? ` · turn ${entry.source_turn}` : ''}
              {entry.source_session ? ` · ${entry.source_session}` : ''}
            </p>
          ) : null}
          {entry.tags.length > 0 ? <p>标签：{entry.tags.join(' / ')}</p> : null}
          <p className="font-mono break-all">id: {entry.id}</p>
        </div>
      ) : null}
    </li>
  );
}
