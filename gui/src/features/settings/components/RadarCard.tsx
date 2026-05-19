import { useState } from 'react';

import { useRadarStatus, useStartRadar, useStopRadar } from '../hooks/useRadar';

/**
 * Settings card for the Ecosystem Radar (生态雷达) background scheduler.
 *
 * The radar runs as a detached subprocess (scripts/radar_runner.py) that
 * polls four sources every 2 h, pushes critical signals to Feishu, and
 * emits a daily digest. This card mirrors that lifecycle in the GUI so
 * the user doesn't have to drop into a terminal to toggle it.
 */
export function RadarCard(): JSX.Element {
  const status = useRadarStatus();
  const startMut = useStartRadar();
  const stopMut = useStopRadar();
  const [logOpen, setLogOpen] = useState(false);

  const alive = status.data?.alive ?? false;
  const pid = status.data?.pid ?? 0;
  const logTail = status.data?.log_tail ?? [];
  const logPath = status.data?.log_path ?? '';

  const lastError =
    (startMut.error && String(startMut.error)) ||
    (stopMut.error && String(stopMut.error)) ||
    (status.error && String(status.error)) ||
    '';

  const lastAction =
    startMut.data?.message ?? stopMut.data?.message ?? '';

  const isBusy = startMut.isPending || stopMut.isPending;

  return (
    <fieldset className="rounded-md border border-border p-3">
      <legend className="px-1 text-sm font-medium">生态雷达（行业热点 → 飞书）</legend>

      <div className="flex flex-col gap-3 pt-1">
        <div className="flex items-center gap-3">
          <span
            aria-label={alive ? 'running' : 'stopped'}
            className={`inline-block h-2.5 w-2.5 rounded-full ${
              alive ? 'bg-emerald-500' : 'bg-muted-foreground/60'
            }`}
          />
          <span className="text-sm">
            {alive ? `运行中 · PID ${pid}` : '未运行'}
          </span>
          {status.isFetching ? (
            <span className="text-xs text-muted-foreground">刷新中…</span>
          ) : null}
        </div>

        <p className="text-xs text-muted-foreground">
          每 2h 扫四个源（GitHub releases / trending、HN AI、Grok 实时）→ critical 即时推飞书；
          每天 21:07 一条 digest；工作日 09:13 选 top1 进 PoC 候选。
          细节见 <code>memory/ecosystem_radar_sop.md</code>。
        </p>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={alive || isBusy}
            onClick={() => startMut.mutate()}
            className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {startMut.isPending ? '启动中…' : '启动'}
          </button>
          <button
            type="button"
            disabled={!alive || isBusy}
            onClick={() => stopMut.mutate()}
            className="px-3 py-1 text-sm rounded border border-border hover:bg-accent disabled:opacity-50"
          >
            {stopMut.isPending ? '停止中…' : '停止'}
          </button>
          <button
            type="button"
            onClick={() => setLogOpen((v) => !v)}
            className="ml-auto px-3 py-1 text-xs rounded border border-border hover:bg-accent"
          >
            {logOpen ? '收起日志' : '查看日志'}
          </button>
        </div>

        {lastAction ? (
          <p className="text-xs text-muted-foreground">{lastAction}</p>
        ) : null}
        {lastError ? (
          <p className="text-xs text-destructive">{lastError}</p>
        ) : null}

        {logOpen ? (
          <div className="rounded border border-border bg-muted/30 p-2">
            <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
              <span>{logPath || '(无日志文件)'}</span>
              <span>近 {logTail.length} 行</span>
            </div>
            <pre className="max-h-48 overflow-auto text-[11px] leading-relaxed whitespace-pre-wrap break-all">
              {logTail.length > 0 ? logTail.join('\n') : '(尚无日志)'}
            </pre>
          </div>
        ) : null}
      </div>
    </fieldset>
  );
}
