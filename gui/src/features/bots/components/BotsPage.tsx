import { useEffect, useRef, useState } from 'react';

import { useLogStream } from '@/lib/useLogStream';

import { useBotLog } from '../hooks/useBots';

interface BotLogDrawerProps {
  botKey: string;
  onClose: () => void;
}

/**
 * Bot log viewer. Defaults to a live SSE tail (pushes appends as soon as the
 * bot writes to its log file). Falls back to a one-shot snapshot (useBotLog)
 * on demand for users who prefer that view (e.g. for quick sharing).
 */
export function BotLogDrawer({ botKey, onClose }: BotLogDrawerProps): JSX.Element {
  const [mode, setMode] = useState<'live' | 'snapshot'>('live');
  const stream = useLogStream(mode === 'live' ? `/api/bots/${botKey}/log/stream` : null);
  const snapshot = useBotLog(mode === 'snapshot' ? botKey : null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when new live lines land.
  useEffect(() => {
    if (mode !== 'live') return;
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [mode, stream.lines.length]);

  const lines = mode === 'live' ? stream.lines : snapshot.data?.lines ?? [];
  const isLoading = mode === 'snapshot' && snapshot.isLoading;
  const errorText = mode === 'live' ? stream.error : snapshot.error ? String(snapshot.error) : null;
  const exists = mode === 'live'
    ? stream.status !== 'idle' && stream.status !== 'error'
    : (snapshot.data?.exists ?? false);

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl h-2/3 rounded-t-lg border border-border bg-background flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div>
            <h3 className="font-medium">
              日志：{botKey}{' '}
              <span className="text-xs text-muted-foreground">
                {mode === 'live' ? `· ${stream.status}` : null}
              </span>
            </h3>
            <p className="text-xs text-muted-foreground">
              {mode === 'live' ? 'SSE 实时模式' : '快照模式'} ·
              {snapshot.data?.path ?? '(等待日志文件)'}
            </p>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setMode((m) => (m === 'live' ? 'snapshot' : 'live'))}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
            >
              {mode === 'live' ? '切换快照' : '切换实时'}
            </button>
            {mode === 'live' && (stream.status === 'closed' || stream.status === 'error') ? (
              <button
                type="button"
                onClick={stream.reconnect}
                className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
              >
                重连
              </button>
            ) : null}
            {mode === 'snapshot' ? (
              <button
                type="button"
                onClick={() => void snapshot.refetch()}
                className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
              >
                刷新
              </button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
            >
              关闭
            </button>
          </div>
        </div>
        <div
          ref={scrollerRef}
          className="flex-1 overflow-auto bg-muted/40 p-3 font-mono text-xs"
        >
          {isLoading ? <p className="text-muted-foreground">加载中…</p> : null}
          {errorText ? <p className="text-destructive">{errorText}</p> : null}
          {!exists && lines.length === 0 ? (
            <p className="text-muted-foreground">日志文件还不存在（bot 尚未运行）。</p>
          ) : null}
          {exists && lines.length === 0 ? (
            <p className="text-muted-foreground">空文件。等待新日志…</p>
          ) : null}
          {lines.map((line, i) => (
            <div key={i} className="whitespace-pre-wrap break-all">
              {line}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

interface BotsPageProps {}

export function BotsPage(_props: BotsPageProps = {}): JSX.Element {
  const [logKey, setLogKey] = useState<string | null>(null);
  return (
    <>
      <BotsTable onShowLog={(k) => setLogKey(k)} />
      {logKey ? <BotLogDrawer botKey={logKey} onClose={() => setLogKey(null)} /> : null}
    </>
  );
}

import { useBots, useInstallBotSdk, useLlmOptions, useSetBotLlmBinding, useStartBot, useStopBot } from '../hooks/useBots';

interface BotsTableProps {
  onShowLog: (key: string) => void;
}

// Bot keys whose subprocess actually honors WLWL_BOT_LLM_BINDING today.
// Mirrors _LLM_BINDING_SUPPORTED_BOTS in launcher/api_server.py — extend
// both sets together when more frontends learn the protocol.
const LLM_BINDABLE_BOTS = new Set(['feishu', 'feishu_concierge']);

function BotsTable({ onShowLog }: BotsTableProps): JSX.Element {
  const bots = useBots();
  const start = useStartBot();
  const stop = useStopBot();
  const install = useInstallBotSdk();
  const llmOptions = useLlmOptions();
  const setBinding = useSetBotLlmBinding();

  return (
    <div className="flex flex-col gap-4 p-4 max-w-4xl mx-auto">
      <header>
        <h1 className="text-xl font-semibold">Bots</h1>
        {/* footer note: literal-string wrap so the angle-bracketed CLI hint
            doesn't get parsed as JSX (HMR-nudge after the post-mykey rewrite) */}
        <p className="text-sm text-muted-foreground">
          {'每 3 秒刷新；🟢 本 launcher / 🟢 外部进程已接管 / 🟡 孤儿无法识别 / ⚪ 已停。凭据编辑请用上方 「Bot 凭据」 卡或 `python -m launcher.config set bots.<bot>.<field> ...`。'}
        </p>
      </header>

      {bots.isLoading ? <p className="text-muted-foreground">加载中…</p> : null}
      {bots.error ? (
        <p className="text-destructive text-sm">后端连接失败：{String(bots.error)}</p>
      ) : null}

      {bots.data ? (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-left border-b border-border">
              <th className="py-2 px-2">Bot</th>
              <th className="py-2 px-2">配置</th>
              <th className="py-2 px-2">LLM</th>
              <th className="py-2 px-2">状态</th>
              <th className="py-2 px-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {bots.data.bots.map((bot) => {
              let cfgText = '✅';
              let cfgTip = '已配置';
              if (!bot.configured) {
                cfgText = '❌';
                cfgTip = '缺字段: ' + bot.missing_fields.join(', ');
              } else if (!bot.sdk_installed) {
                cfgText = '⚠️';
                cfgTip = '缺 SDK: ' + bot.missing_modules.join(', ');
              }
              const adopted = bot.running_external && bot.lock_holder_pid != null;
              const stateText = bot.running_self
                ? '🟢 运行中（本 launcher）'
                : adopted
                  ? `🟢 运行中（已接管 pid=${bot.lock_holder_pid}）`
                  : bot.running_external
                    ? '🟡 孤儿进程占端口（无法识别 PID）'
                    : '⚪ 已停';
              const startable = bot.configured && bot.sdk_installed && !bot.running;
              const bindable = LLM_BINDABLE_BOTS.has(bot.key);
              // Detect a stale binding (e.g. profile renamed / config deleted)
              // so the dropdown can flag it visibly.
              const bindKind = bot.llm_binding.split(':')[0] ?? '';
              const bindName = bot.llm_binding.split(':')[1] ?? '';
              const knownConfigs = llmOptions.data?.configs ?? [];
              const knownProfiles = llmOptions.data?.profiles ?? [];
              const isStale =
                bindable && bot.llm_binding !== '' && !(
                  (bindKind === 'config' && knownConfigs.some((c) => c.name === bindName)) ||
                  (bindKind === 'profile' && knownProfiles.some((p) => p.name === bindName))
                );
              return (
                <tr key={bot.key} className="border-b border-border last:border-0">
                  <td className="py-2 px-2">
                    <div className="font-medium">{bot.display_name}</div>
                    <div className="text-xs text-muted-foreground">{bot.key}</div>
                  </td>
                  <td className="py-2 px-2" title={cfgTip}>{cfgText}</td>
                  <td className="py-2 px-2">
                    {bindable ? (
                      <div className="flex flex-col gap-0.5">
                        <select
                          className="text-xs px-1 py-0.5 rounded border border-border bg-background"
                          value={bot.llm_binding}
                          disabled={setBinding.isPending}
                          onChange={(e) =>
                            setBinding.mutate({ key: bot.key, binding: e.target.value })
                          }
                        >
                          <option value="">（默认 / 首条 config）</option>
                          {isStale ? (
                            <option value={bot.llm_binding}>
                              ⚠️ {bot.llm_binding}（已失效）
                            </option>
                          ) : null}
                          {knownProfiles.length > 0 ? (
                            <optgroup label="Profile（fallback chain）">
                              {knownProfiles.map((p) => (
                                <option key={`p-${p.name}`} value={`profile:${p.name}`}>
                                  profile:{p.name}（{p.members.join(' → ') || '空'}）
                                </option>
                              ))}
                            </optgroup>
                          ) : null}
                          {knownConfigs.length > 0 ? (
                            <optgroup label="单个 Config">
                              {knownConfigs.map((c) => (
                                <option key={`c-${c.name}`} value={`config:${c.name}`}>
                                  config:{c.name}
                                </option>
                              ))}
                            </optgroup>
                          ) : null}
                        </select>
                        {bot.running && bot.llm_binding ? (
                          <span className="text-[10px] text-amber-600">改后重启生效</span>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="py-2 px-2">{stateText}</td>
                  <td className="py-2 px-2 text-right">
                    <div className="inline-flex gap-1">
                      {!bot.sdk_installed && bot.missing_modules.length > 0 ? (
                        <button
                          type="button"
                          onClick={() => install.mutate(bot.key)}
                          disabled={install.isPending}
                          className="px-2 py-0.5 text-xs rounded border border-amber-500/40 text-amber-700 hover:bg-amber-50 disabled:opacity-40"
                          title={`pip install ${bot.missing_modules.join(' ')}`}
                        >
                          {install.isPending && install.variables === bot.key
                            ? '安装中…'
                            : '安装 SDK'}
                        </button>
                      ) : null}
                      <button
                        type="button"
                        onClick={() => start.mutate(bot.key)}
                        disabled={!startable || start.isPending}
                        className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent disabled:opacity-40"
                      >
                        启动
                      </button>
                      <button
                        type="button"
                        onClick={() => stop.mutate(bot.key)}
                        disabled={!(bot.running_self || adopted) || stop.isPending}
                        className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent disabled:opacity-40"
                      >
                        停止
                      </button>
                      <button
                        type="button"
                        onClick={() => onShowLog(bot.key)}
                        className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
                      >
                        日志
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}

      {start.isError ? (
        <p className="text-xs text-destructive">启动失败：{String(start.error)}</p>
      ) : null}
      {install.isError ? (
        <p className="text-xs text-destructive">安装失败：{String(install.error)}</p>
      ) : null}
      {install.data ? (
        <p className={`text-xs ${install.data.ok ? 'text-emerald-600' : 'text-destructive'}`}>
          {install.data.ok
            ? `✅ 已安装：${install.data.packages?.join(', ')}`
            : `❌ 安装失败 (returncode=${install.data.returncode}) — 详见 ${install.data.log_path}`}
        </p>
      ) : null}
      {setBinding.isError ? (
        <p className="text-xs text-destructive">LLM 绑定保存失败：{String(setBinding.error)}</p>
      ) : null}
      {setBinding.data ? (
        <p className="text-xs text-emerald-600">
          ✅ {setBinding.data.key} 绑定已保存
          {setBinding.data.binding ? ` (${setBinding.data.binding})` : ''}
          {setBinding.data.restart_required ? '；重启 bot 生效' : ''}
        </p>
      ) : null}
    </div>
  );
}
