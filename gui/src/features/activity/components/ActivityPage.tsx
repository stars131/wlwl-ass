import { useEffect, useMemo, useRef, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { ProcessesCard } from '@/features/processes';
import { useLogStream } from '@/lib/useLogStream';

import { exportTrajectory, type TrajectoryExport } from '../api/activityApi';
import { useRecentActivity } from '../hooks/useActivity';
import { activityEventSchema, type ActivityEvent, type ActivityPhase } from '../types';

const MAX_KEPT = 1000;

function parseEventLine(line: string): ActivityEvent | null {
  try {
    const obj = JSON.parse(line);
    const parsed = activityEventSchema.safeParse(obj);
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

const PHASE_STYLE: Record<ActivityPhase, string> = {
  tool_start: 'text-blue-600 dark:text-blue-400',
  tool_end: 'text-emerald-600 dark:text-emerald-400',
  turn_end: 'text-purple-600 dark:text-purple-400',
};

const PHASE_LABEL: Record<ActivityPhase, string> = {
  tool_start: '▶ start',
  tool_end: '✓ end',
  turn_end: '⏎ turn',
};

function compactArgs(args: Record<string, unknown> | undefined): string {
  if (!args) return '';
  try {
    const s = JSON.stringify(args);
    return s.length > 200 ? s.slice(0, 200) + '…' : s;
  } catch {
    return '';
  }
}

/**
 * Activity tab — global feed of every tool dispatch + turn boundary across
 * all running agent processes. Surfaces what wlwl-ass is currently
 * doing so the user can audit / debug / spot loops.
 *
 * Snapshot mode: paginated read of /api/activity?limit=N, polled every 5s.
 * Live mode: SSE tail of today's JSONL file via /api/activity/stream.
 */
export function ActivityPage(): JSX.Element {
  const { t } = useTranslation();
  const [mode, setMode] = useState<'live' | 'snapshot'>('live');
  const [filter, setFilter] = useState<string>('');
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const [liveEvents, setLiveEvents] = useState<ActivityEvent[]>([]);
  const liveBufferRef = useRef<ActivityEvent[]>([]);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Trajectory export (#32) — a one-shot button + status banner. Stays
  // in this component because the user expects "what just happened" and
  // "let me grab a copy" in the same place.
  const [exportState, setExportState] = useState<
    | { state: 'idle' }
    | { state: 'pending' }
    | { state: 'done'; data: TrajectoryExport }
    | { state: 'error'; error: string }
  >({ state: 'idle' });
  const onExport = async () => {
    setExportState({ state: 'pending' });
    try {
      const data = await exportTrajectory({ max_blob_chars: 200, include_args: true });
      setExportState({ state: 'done', data });
    } catch (err) {
      setExportState({ state: 'error', error: String(err) });
    }
  };

  const snapshot = useRecentActivity(500);
  const stream = useLogStream(mode === 'live' ? '/api/activity/stream' : null, {
    maxLines: MAX_KEPT * 2,
  });

  // Convert raw SSE lines into ActivityEvent[]; only re-parse on growth.
  useEffect(() => {
    if (mode !== 'live') return;
    if (paused) return;
    const parsed: ActivityEvent[] = [];
    for (const line of stream.lines) {
      const ev = parseEventLine(line);
      if (ev) parsed.push(ev);
    }
    const trimmed =
      parsed.length > MAX_KEPT ? parsed.slice(parsed.length - MAX_KEPT) : parsed;
    liveBufferRef.current = trimmed;
    setLiveEvents(trimmed);
  }, [stream.lines, mode, paused]);

  const events = mode === 'live' ? liveEvents : snapshot.data?.events ?? [];

  const filtered = useMemo(() => {
    if (!filter.trim()) return events;
    const needle = filter.trim().toLowerCase();
    return events.filter((e) => {
      if (e.tool && e.tool.toLowerCase().includes(needle)) return true;
      if (e.summary && e.summary.toLowerCase().includes(needle)) return true;
      return false;
    });
  }, [events, filter]);

  useEffect(() => {
    if (!autoScroll) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [filtered.length, autoScroll]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    setAutoScroll(atBottom);
  };

  return (
    <div className="flex flex-col h-full p-4 gap-3">
      <header className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">{t('activity.title')}</h1>
          <p className="text-xs text-muted-foreground">{t('activity.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <button
            type="button"
            onClick={() => setMode(mode === 'live' ? 'snapshot' : 'live')}
            className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
            title={mode === 'live' ? t('activity.switchSnapshot') : t('activity.switchLive')}
          >
            {mode === 'live' ? t('activity.modeLive') : t('activity.modeSnapshot')}
          </button>
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={t('activity.filterPlaceholder')}
            className="rounded border border-border bg-background px-2 py-1 text-xs w-44"
          />
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
          >
            {paused ? t('activity.resume') : t('activity.pause')}
          </button>
          {mode === 'live' && (stream.status === 'closed' || stream.status === 'error') ? (
            <button
              type="button"
              onClick={stream.reconnect}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
            >
              {t('common.reconnect')}
            </button>
          ) : null}
          <button
            type="button"
            onClick={onExport}
            disabled={exportState.state === 'pending'}
            className="px-2 py-1 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
            title="把 activity log 压缩成 JSONL trajectories（每个 run 一行），可作为训练数据/复现证据"
          >
            {exportState.state === 'pending' ? '导出中…' : '导出 trajectory'}
          </button>
        </div>
      </header>

      {exportState.state === 'done' ? (
        <div className="text-xs rounded border border-emerald-500/40 bg-emerald-50 dark:bg-emerald-950/30 px-2 py-1.5 flex items-baseline justify-between gap-2 flex-wrap">
          <span className="text-emerald-700 dark:text-emerald-400">
            ✅ 已导出 {exportState.data.count} 个 run 到 <code className="font-mono">{exportState.data.path}</code>
          </span>
          <span className="text-muted-foreground">
            skills: {Array.from(new Set(exportState.data.runs.flatMap((r) => r.skills_used))).slice(0, 6).join(', ') || '(none)'}
          </span>
        </div>
      ) : null}
      {exportState.state === 'error' ? (
        <p className="text-xs text-destructive">导出失败：{exportState.error}</p>
      ) : null}

      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        <span>
          {t('activity.shown', { shown: filtered.length, total: events.length })}
        </span>
        {mode === 'live' ? (
          <span>
            {stream.status === 'open' && '🟢 live'}
            {stream.status === 'connecting' && '🟡 connecting'}
            {stream.status === 'closed' && '⚪ closed'}
            {stream.status === 'error' && '🔴 error'}
            {paused && ' · ⏸ paused'}
          </span>
        ) : (
          <span>{snapshot.isFetching ? '🟡 refreshing' : '🟢 polled'}</span>
        )}
        {!autoScroll && mode === 'live' ? (
          <button
            type="button"
            onClick={() => setAutoScroll(true)}
            className="underline hover:no-underline"
          >
            {t('activity.jumpLatest')}
          </button>
        ) : null}
      </div>

      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="flex-1 overflow-auto rounded-md border border-border bg-muted/20 font-mono text-xs"
      >
        {filtered.length === 0 ? (
          <p className="p-4 text-muted-foreground">
            {snapshot.isLoading
              ? t('common.loading')
              : t(events.length === 0 ? 'activity.empty' : 'activity.noMatch')}
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {filtered.map((e, i) => (
              <li key={`${e.ts}-${i}`} className="px-3 py-1.5 flex items-baseline gap-3">
                <span className="text-muted-foreground tabular-nums shrink-0">
                  {formatTime(e.ts)}
                </span>
                <span className={`shrink-0 ${PHASE_STYLE[e.phase]}`}>
                  {PHASE_LABEL[e.phase]}
                </span>
                {e.turn !== undefined ? (
                  <span className="text-muted-foreground shrink-0">T{e.turn}</span>
                ) : null}
                {e.tool ? (
                  <span className="font-semibold shrink-0">{e.tool}</span>
                ) : null}
                <span className="text-muted-foreground truncate">
                  {e.phase === 'turn_end'
                    ? e.summary || JSON.stringify(e.exit_reason)
                    : e.phase === 'tool_end'
                    ? e.elapsed_s !== undefined
                      ? `${e.elapsed_s.toFixed(2)}s`
                      : ''
                    : compactArgs(e.args)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <ProcessesCard />
    </div>
  );
}
