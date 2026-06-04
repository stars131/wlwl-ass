import { useEffect, useRef, useState } from 'react';

import {
  useRadarConfig,
  useRadarStatus,
  useSaveRadarConfig,
  useStartRadar,
  useStopRadar,
} from '../hooks/useRadar';
import type { RadarConfig } from '../api/radarApi';

interface RadarDraft {
  feishu_app_id: string;
  feishu_app_secret: string;
  notify_to: string;
  quiet_hours: string;
  watchlist: string;
  grok_api_key: string;
  grok_url: string;
  grok_model: string;
  tavily_api_key: string;
  tavily_url: string;
}

function toDraft(config: RadarConfig): RadarDraft {
  return {
    feishu_app_id: config.feishu_app_id,
    feishu_app_secret: config.feishu_app_secret,
    notify_to: config.notify_to,
    quiet_hours: config.quiet_hours,
    watchlist: config.watchlist.join('\n'),
    grok_api_key: config.grok_api_key,
    grok_url: config.grok_url,
    grok_model: config.grok_model,
    tavily_api_key: config.tavily_api_key,
    tavily_url: config.tavily_url,
  };
}

function splitWatchlist(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter((s) => s.includes('/'))
    .slice(0, 20);
}

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
  const config = useRadarConfig();
  const saveConfig = useSaveRadarConfig();
  const startMut = useStartRadar();
  const stopMut = useStopRadar();
  const [logOpen, setLogOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const [draft, setDraft] = useState<RadarDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);

  useEffect(() => {
    if (config.data?.config && !dirtyRef.current) {
      setDraft(toDraft(config.data.config));
    }
  }, [config.data?.config]);

  const alive = status.data?.alive ?? false;
  const pid = status.data?.pid ?? 0;
  const pidSource = status.data?.pid_source ?? '';
  const health = status.data?.config ?? config.data?.status.config;
  const ready = health?.ready ?? false;
  const missing = health?.missing ?? [];
  const warnings = health?.warnings ?? [];
  const logTail = status.data?.log_tail ?? [];
  const logPath = status.data?.log_path ?? '';

  const lastError =
    (startMut.error && String(startMut.error)) ||
    (stopMut.error && String(stopMut.error)) ||
    (config.error && String(config.error)) ||
    (saveConfig.error && String(saveConfig.error)) ||
    (status.error && String(status.error)) ||
    '';

  const lastAction =
    startMut.data?.message ??
    stopMut.data?.message ??
    (saveConfig.isSuccess ? '雷达配置已保存' : '');

  const isBusy = startMut.isPending || stopMut.isPending || saveConfig.isPending;

  const updateDraft = <K extends keyof RadarDraft>(key: K, value: RadarDraft[K]) => {
    dirtyRef.current = true;
    setDirty(true);
    setDraft((d) => (d === null ? d : { ...d, [key]: value }));
  };

  const onSaveConfig = () => {
    if (!draft) return;
    saveConfig.mutate(
      {
        feishu_app_id: draft.feishu_app_id.trim(),
        feishu_app_secret: draft.feishu_app_secret.trim(),
        notify_to: draft.notify_to.trim(),
        quiet_hours: draft.quiet_hours.trim(),
        watchlist: splitWatchlist(draft.watchlist),
        grok_api_key: draft.grok_api_key.trim(),
        grok_url: draft.grok_url.trim(),
        grok_model: draft.grok_model.trim(),
        tavily_api_key: draft.tavily_api_key.trim(),
        tavily_url: draft.tavily_url.trim(),
      },
      {
        onSuccess: () => {
          dirtyRef.current = false;
          setDirty(false);
        },
      },
    );
  };

  return (
    <fieldset className="rounded-md border border-border p-3">
      <legend className="px-1 text-sm font-medium">生态雷达（行业热点 → 飞书）</legend>

      <div className="flex flex-col gap-3 pt-1">
        <div className="flex flex-wrap items-center gap-3">
          <StatusDot ok={alive} label={alive ? 'running' : 'stopped'} />
          <span className="text-sm">{alive ? `运行中 · PID ${pid || '未知'}` : '未运行'}</span>
          {pidSource === 'lock_port' ? (
            <span className="text-xs text-amber-600 dark:text-amber-400">由锁端口识别</span>
          ) : null}
          <StatusDot ok={ready} label={ready ? 'configured' : 'not configured'} />
          <span className="text-sm">{ready ? '配置就绪' : '配置缺失'}</span>
          {status.isFetching || config.isFetching ? (
            <span className="text-xs text-muted-foreground">刷新中…</span>
          ) : null}
        </div>

        {missing.length > 0 ? (
          <p className="text-xs text-destructive">缺少：{missing.join('、')}</p>
        ) : null}
        {warnings.length > 0 ? (
          <p className="text-xs text-amber-600 dark:text-amber-400">{warnings.join('；')}</p>
        ) : null}

        <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground sm:grid-cols-5">
          <span>接收人：{health?.feishu.notify_to || '未设置'}</span>
          <span>静默：{health?.quiet_hours || '22-8'}</span>
          <span>Grok：{health?.sources.grok ? health.sources.grok_model : '未配置'}</span>
          <span>Tavily：{health?.sources.tavily ? '已配置' : '未配置'}</span>
          <span>watchlist：{health?.watchlist_count ?? 0} 个</span>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={alive || isBusy || !ready}
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
            onClick={() => setConfigOpen((v) => !v)}
            className="px-3 py-1 text-xs rounded border border-border hover:bg-accent"
          >
            {configOpen ? '收起配置' : '配置'}
          </button>
          <button
            type="button"
            onClick={() => setLogOpen((v) => !v)}
            className="ml-auto px-3 py-1 text-xs rounded border border-border hover:bg-accent"
          >
            {logOpen ? '收起日志' : '查看日志'}
          </button>
        </div>

        {!ready ? (
          <p className="text-xs text-muted-foreground">补齐飞书凭据和通知接收人后才能启动。</p>
        ) : null}

        {configOpen ? (
          <div className="rounded border border-border bg-muted/20 p-3">
            {draft ? (
              <div className="grid gap-3">
                <div className="grid gap-3 sm:grid-cols-2">
                  <TextField
                    label="飞书 App ID"
                    value={draft.feishu_app_id}
                    onChange={(v) => updateDraft('feishu_app_id', v)}
                  />
                  <TextField
                    label="飞书 App Secret"
                    type="password"
                    value={draft.feishu_app_secret}
                    onChange={(v) => updateDraft('feishu_app_secret', v)}
                  />
                  <TextField
                    label="通知接收人 open_id"
                    value={draft.notify_to}
                    placeholder="ou_xxx"
                    onChange={(v) => updateDraft('notify_to', v)}
                  />
                  <TextField
                    label="静默时段"
                    value={draft.quiet_hours}
                    placeholder="22-8 或 off"
                    onChange={(v) => updateDraft('quiet_hours', v)}
                  />
                  <TextField
                    label="Grok / xAI API Key"
                    type="password"
                    value={draft.grok_api_key}
                    onChange={(v) => updateDraft('grok_api_key', v)}
                  />
                  <TextField
                    label="Grok API URL"
                    value={draft.grok_url}
                    placeholder="https://api.x.ai/v1"
                    onChange={(v) => updateDraft('grok_url', v)}
                  />
                  <TextField
                    label="Grok 模型"
                    value={draft.grok_model}
                    onChange={(v) => updateDraft('grok_model', v)}
                  />
                  <TextField
                    label="Tavily API Key"
                    type="password"
                    value={draft.tavily_api_key}
                    onChange={(v) => updateDraft('tavily_api_key', v)}
                  />
                  <TextField
                    label="Tavily URL"
                    value={draft.tavily_url}
                    placeholder="https://api.tavily.com/search"
                    onChange={(v) => updateDraft('tavily_url', v)}
                  />
                </div>
                <label className="flex flex-col gap-1 text-sm">
                  <span className="text-xs text-muted-foreground">watchlist 仓库（每行一个 owner/repo）</span>
                  <textarea
                    rows={5}
                    value={draft.watchlist}
                    onChange={(e) => updateDraft('watchlist', e.target.value)}
                    className="rounded-md border border-border bg-background px-2 py-1 font-mono text-xs"
                  />
                </label>
                <div className="flex justify-end">
                  {dirty ? (
                    <span className="mr-auto self-center text-xs text-muted-foreground">
                      有未保存更改
                    </span>
                  ) : null}
                  <button
                    type="button"
                    onClick={onSaveConfig}
                    disabled={saveConfig.isPending}
                    className="px-3 py-1 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    {saveConfig.isPending ? '保存中…' : '保存雷达配置'}
                  </button>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">加载雷达配置中…</p>
            )}
          </div>
        ) : null}

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

interface StatusDotProps {
  ok: boolean;
  label: string;
}
function StatusDot({ ok, label }: StatusDotProps): JSX.Element {
  return (
    <span
      aria-label={label}
      className={`inline-block h-2.5 w-2.5 rounded-full ${
        ok ? 'bg-emerald-500' : 'bg-muted-foreground/60'
      }`}
    />
  );
}

interface TextFieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: 'text' | 'password';
}
function TextField({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
}: TextFieldProps): JSX.Element {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-xs text-muted-foreground">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-border bg-background px-2 py-1 font-mono text-xs"
      />
    </label>
  );
}
