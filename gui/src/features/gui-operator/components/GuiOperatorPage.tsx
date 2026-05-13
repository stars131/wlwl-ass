import { useMemo, useState } from 'react';

import type { GuiRun, GuiStep } from '../types';
import {
  useGuiRuns,
  useGuiSidecarStatus,
  usePauseGuiRun,
  useResumeGuiRun,
  useStartGuiRun,
  useStopGuiRun,
} from '../hooks/useGuiOperator';

function statusClass(status: string): string {
  if (['running', 'queued'].includes(status)) return 'text-blue-600 dark:text-blue-400';
  if (status === 'paused') return 'text-amber-600 dark:text-amber-400';
  if (['finished', 'dry_run'].includes(status)) return 'text-emerald-600 dark:text-emerald-400';
  if (['error', 'stopped', 'stopping'].includes(status)) return 'text-destructive';
  return 'text-muted-foreground';
}

function stepSummary(step: GuiStep): string {
  const status = step.status ?? '';
  const action = step.action_text ?? step.action ?? '';
  const error = step.error ?? '';
  return [status, action || error].filter(Boolean).join(' ');
}

function screenshotSrc(step: GuiStep): string {
  const observe = step.observe;
  if (observe && typeof observe === 'object' && 'base64' in observe) {
    const b64 = (observe as { base64?: unknown }).base64;
    if (typeof b64 === 'string' && b64) return `data:image/png;base64,${b64}`;
  }
  return '';
}

function latestRun(runs: GuiRun[]): GuiRun | null {
  return runs.length ? runs[0] ?? null : null;
}

export function GuiOperatorPage(): JSX.Element {
  const runs = useGuiRuns();
  const sidecar = useGuiSidecarStatus();
  const start = useStartGuiRun();
  const pause = usePauseGuiRun();
  const resume = useResumeGuiRun();
  const stop = useStopGuiRun();

  const [instruction, setInstruction] = useState('');
  const [maxLoop, setMaxLoop] = useState(5);
  const [loopWait, setLoopWait] = useState(1);
  const [dryRun, setDryRun] = useState(true);
  const [includeBase64, setIncludeBase64] = useState(true);
  const [allScreens, setAllScreens] = useState(false);
  const [backend, setBackend] = useState('auto');
  const [selectedRunId, setSelectedRunId] = useState<string>('');

  const rows = runs.data?.runs ?? [];
  const selected = useMemo(
    () => rows.find((r) => r.run_id === selectedRunId) ?? latestRun(rows),
    [rows, selectedRunId],
  );

  const onStart = () => {
    const text = instruction.trim();
    if (!text) return;
    start.mutate(
      {
        instruction: text,
        max_loop: maxLoop,
        loop_wait: loopWait,
        dry_run: dryRun,
        backend,
        all_screens: allScreens,
        include_base64: includeBase64,
      },
      {
        onSuccess: (run) => setSelectedRunId(run.run_id),
      },
    );
  };

  const steps = selected?.steps ?? [];
  const lastStep = steps.length ? steps[steps.length - 1] : null;
  const previewSrc = lastStep ? screenshotSrc(lastStep) : '';

  return (
    <div className="h-full p-4 grid grid-cols-[minmax(280px,360px)_1fr] gap-4">
      <section className="rounded-md border border-border bg-card p-3 flex flex-col gap-3">
        <header>
          <h1 className="text-xl font-semibold">视觉操作</h1>
          <p className="text-xs text-muted-foreground">gui_operator / computer-use</p>
        </header>

        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs text-muted-foreground">任务</span>
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            rows={5}
            className="rounded border border-border bg-background px-2 py-1 text-sm resize-none"
          />
        </label>

        <div className="grid grid-cols-2 gap-2 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">max loop</span>
            <input
              type="number"
              min={1}
              max={50}
              value={maxLoop}
              onChange={(e) => setMaxLoop(Number(e.target.value) || 1)}
              className="rounded border border-border bg-background px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">loop wait</span>
            <input
              type="number"
              min={0}
              step={0.25}
              value={loopWait}
              onChange={(e) => setLoopWait(Number(e.target.value) || 0)}
              className="rounded border border-border bg-background px-2 py-1"
            />
          </label>
        </div>

        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs text-muted-foreground">backend</span>
          <select
            value={backend}
            onChange={(e) => setBackend(e.target.value)}
            className="rounded border border-border bg-background px-2 py-1"
          >
            <option value="auto">auto</option>
            <option value="openai">openai</option>
            <option value="claude">claude</option>
          </select>
        </label>

        <div className="flex flex-col gap-2 text-sm">
          <label className="flex items-center justify-between gap-2 rounded border border-border px-2 py-1">
            <span>dry run</span>
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
          </label>
          <label className="flex items-center justify-between gap-2 rounded border border-border px-2 py-1">
            <span>preview image</span>
            <input
              type="checkbox"
              checked={includeBase64}
              onChange={(e) => setIncludeBase64(e.target.checked)}
            />
          </label>
          <label className="flex items-center justify-between gap-2 rounded border border-border px-2 py-1">
            <span>all screens</span>
            <input
              type="checkbox"
              checked={allScreens}
              onChange={(e) => setAllScreens(e.target.checked)}
            />
          </label>
        </div>

        <button
          type="button"
          onClick={onStart}
          disabled={start.isPending || !instruction.trim()}
          className="rounded border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50"
        >
          {start.isPending ? '启动中' : '开始'}
        </button>

        {start.error ? (
          <p className="text-xs text-destructive">{String(start.error)}</p>
        ) : null}

        <div className="border-t border-border pt-3 text-xs text-muted-foreground">
          <div>sidecar: {sidecar.data?.configured ? sidecar.data.url : 'optional'}</div>
          <div>node: {sidecar.data?.node_available ? 'available' : 'not found'}</div>
        </div>
      </section>

      <section className="min-w-0 flex flex-col gap-3">
        <div className="rounded-md border border-border bg-card p-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-xs text-muted-foreground">当前 run</div>
            <div className="font-mono text-sm truncate">{selected?.run_id ?? '(none)'}</div>
            {selected ? (
              <div className={`text-xs ${statusClass(selected.status)}`}>{selected.status}</div>
            ) : null}
          </div>
          {selected ? (
            <div className="flex items-center gap-2 text-xs">
              <button
                type="button"
                onClick={() => pause.mutate(selected.run_id)}
                disabled={!['running', 'queued'].includes(selected.status) || pause.isPending}
                className="rounded border border-border px-2 py-1 hover:bg-accent disabled:opacity-50"
              >
                暂停
              </button>
              <button
                type="button"
                onClick={() => resume.mutate(selected.run_id)}
                disabled={selected.status !== 'paused' || resume.isPending}
                className="rounded border border-border px-2 py-1 hover:bg-accent disabled:opacity-50"
              >
                恢复
              </button>
              <button
                type="button"
                onClick={() => stop.mutate(selected.run_id)}
                disabled={!['running', 'queued', 'paused', 'stopping'].includes(selected.status) || stop.isPending}
                className="rounded border border-destructive/40 px-2 py-1 text-destructive hover:bg-destructive/10 disabled:opacity-50"
              >
                停止
              </button>
            </div>
          ) : null}
        </div>

        <div className="grid grid-cols-[1fr_320px] gap-3 min-h-0 flex-1">
          <div className="rounded-md border border-border bg-card overflow-auto">
            {rows.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">暂无视觉操作记录。</p>
            ) : (
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="text-left border-b border-border text-muted-foreground">
                    <th className="px-2 py-2">run</th>
                    <th className="px-2 py-2">status</th>
                    <th className="px-2 py-2">steps</th>
                    <th className="px-2 py-2">dir</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((run) => (
                    <tr
                      key={run.run_id}
                      className={`border-b border-border last:border-0 cursor-pointer hover:bg-muted/50 ${
                        selected?.run_id === run.run_id ? 'bg-accent/40' : ''
                      }`}
                      onClick={() => setSelectedRunId(run.run_id)}
                    >
                      <td className="px-2 py-2 font-mono">{run.run_id}</td>
                      <td className={`px-2 py-2 ${statusClass(run.status)}`}>{run.status}</td>
                      <td className="px-2 py-2 tabular-nums">{run.steps.length}</td>
                      <td className="px-2 py-2 truncate max-w-[18rem]" title={run.run_dir}>{run.run_dir}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <aside className="rounded-md border border-border bg-card p-3 overflow-auto">
            <h2 className="text-sm font-semibold mb-2">步骤</h2>
            {previewSrc ? (
              <img src={previewSrc} alt="latest screenshot" className="w-full rounded border border-border mb-3" />
            ) : null}
            {steps.length === 0 ? (
              <p className="text-xs text-muted-foreground">暂无步骤。</p>
            ) : (
              <ol className="flex flex-col gap-2">
                {steps.map((step, index) => {
                  return (
                    <li key={index} className="rounded border border-border p-2">
                      <div className="text-xs font-medium">{index + 1}. {stepSummary(step)}</div>
                      <pre className="mt-1 text-[11px] whitespace-pre-wrap break-all text-muted-foreground">
                        {JSON.stringify(step.parsed ?? step.result ?? step.error ?? step, null, 2)}
                      </pre>
                    </li>
                  );
                })}
              </ol>
            )}
          </aside>
        </div>
      </section>
    </div>
  );
}
