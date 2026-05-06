import { useEffect, useRef } from 'react';

import { useLogStream } from '@/lib/useLogStream';

interface SessionLogDrawerProps {
  projectId: string;
  projectName: string;
  onClose: () => void;
}

/**
 * Session log viewer. Tails the project's launcher log via SSE so output
 * appears live as the project process appends to it.
 *
 * Unlike the bot log drawer, the launcher does not expose a one-shot
 * snapshot endpoint for projects (`GET /api/projects/<id>/log` does not
 * exist; only the `/log/stream` SSE route does). Live tail is therefore the
 * single mode here — keeps the UI simple and matches the source of truth.
 */
export function SessionLogDrawer({
  projectId,
  projectName,
  onClose,
}: SessionLogDrawerProps): JSX.Element {
  const stream = useLogStream(`/api/projects/${encodeURIComponent(projectId)}/log/stream`);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom on new appends (mirrors BotLogDrawer).
  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [stream.lines.length]);

  const isWaiting =
    stream.lines.length === 0 &&
    (stream.status === 'connecting' || stream.status === 'open');

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
          <div className="min-w-0">
            <h3 className="font-medium truncate">
              日志：{projectName}{' '}
              <span className="text-xs text-muted-foreground">· {stream.status}</span>
            </h3>
            <p className="text-xs text-muted-foreground truncate">
              SSE 实时模式 · id={projectId}
            </p>
          </div>
          <div className="flex gap-2 shrink-0">
            {stream.status === 'closed' || stream.status === 'error' ? (
              <button
                type="button"
                onClick={stream.reconnect}
                className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
              >
                重连
              </button>
            ) : null}
            <button
              type="button"
              onClick={stream.clear}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
            >
              清屏
            </button>
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
          {stream.error ? <p className="text-destructive">{stream.error}</p> : null}
          {isWaiting ? (
            <p className="text-muted-foreground">等待日志…（项目尚未输出或未启动）</p>
          ) : null}
          {!isWaiting && stream.lines.length === 0 && stream.status === 'closed' ? (
            <p className="text-muted-foreground">连接已关闭，可点击右上角"重连"继续抓取。</p>
          ) : null}
          {stream.lines.map((line, i) => (
            <div key={i} className="whitespace-pre-wrap break-all">
              {line}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
