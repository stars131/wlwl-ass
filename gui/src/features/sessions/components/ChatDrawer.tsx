import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  abortProjectMessage,
  listProjectMessages,
  sendProjectMessage,
} from '../api/sessionsApi';
import type { ChatArtifact, ChatMessage, Project, RequestMode, TaskStep } from '../types';

interface ChatDrawerProps {
  project: Project;
  onClose: () => void;
}

const MODE_OPTIONS: { value: RequestMode; label: string; title: string }[] = [
  { value: 'auto', label: 'Auto', title: '自动判断对话、任务或成果模式' },
  { value: 'chat', label: 'Chat', title: '只回答，不主动进入操作流程' },
  { value: 'task', label: 'Task', title: '进入可追踪的操作流程' },
  { value: 'canvas', label: 'Canvas', title: '生成可复用成果并打开右侧画布' },
];

export function ChatDrawer({ project, onClose }: ChatDrawerProps): JSX.Element {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [mode, setMode] = useState<RequestMode>('auto');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [aborting, setAborting] = useState(false);
  const [runtimeRunning, setRuntimeRunning] = useState(Boolean(project.running));
  const [error, setError] = useState<string | null>(null);
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  const hasRunningReply = useMemo(
    () => runtimeRunning && messages.some((m) => m.role === 'assistant' && m.status === 'running'),
    [messages, runtimeRunning],
  );

  const artifacts = useMemo(
    () => messages.flatMap((m) => m.artifacts ?? []),
    [messages],
  );
  const latestArtifact = artifacts.at(-1) ?? null;
  const latestArtifactId = latestArtifact?.id ?? null;
  const selectedArtifact =
    artifacts.find((artifact) => artifact.id === selectedArtifactId) ?? latestArtifact;

  const refresh = useCallback(async () => {
    try {
      const data = await listProjectMessages(project.id);
      setMessages(data.messages);
      if (typeof data.running === 'boolean') setRuntimeRunning(data.running);
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [project.id]);

  useEffect(() => {
    setRuntimeRunning(Boolean(project.running));
    setLoading(true);
    void refresh();
  }, [project.id, project.running, refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refresh();
    }, hasRunningReply ? 800 : 2000);
    return () => window.clearInterval(timer);
  }, [hasRunningReply, refresh]);

  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, hasRunningReply]);

  useEffect(() => {
    if (latestArtifact && selectedArtifactId !== latestArtifact.id) {
      setSelectedArtifactId(latestArtifact.id);
      setCanvasOpen(true);
    }
  }, [latestArtifact, latestArtifactId, selectedArtifactId]);

  const openArtifact = (artifact: ChatArtifact) => {
    setSelectedArtifactId(artifact.id);
    setCanvasOpen(true);
  };

  const onSend = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending || hasRunningReply) return;
    setSending(true);
    setError(null);
    try {
      const data = await sendProjectMessage(project.id, text, mode);
      setMessages(data.messages);
      if (typeof data.running === 'boolean') setRuntimeRunning(data.running);
      setInput('');
    } catch (err) {
      setError(String(err));
    } finally {
      setSending(false);
    }
  };

  const onAbort = async () => {
    if (aborting || !hasRunningReply) return;
    setAborting(true);
    setError(null);
    try {
      const data = await abortProjectMessage(project.id);
      setMessages(data.messages);
      if (typeof data.running === 'boolean') setRuntimeRunning(data.running);
    } catch (err) {
      setError(String(err));
    } finally {
      setAborting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex items-stretch bg-black/40" onClick={onClose}>
      <div
        className="ml-auto flex h-full w-full max-w-[1180px] flex-col border-l border-border bg-background"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
          <div className="min-w-0">
            <h3 className="truncate font-medium">对话：{project.name}</h3>
            <p className="truncate text-xs text-muted-foreground">
              {runtimeRunning ? '运行中' : '已停止'} · id={project.id}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {artifacts.length > 0 ? (
              <button
                type="button"
                onClick={() => setCanvasOpen((v) => !v)}
                className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
              >
                {canvasOpen ? '隐藏 Canvas' : '打开 Canvas'}
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => void refresh()}
              className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
            >
              刷新
            </button>
            {hasRunningReply ? (
              <button
                type="button"
                onClick={() => void onAbort()}
                disabled={aborting}
                className="rounded border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
              >
                {aborting ? '停止中' : '停止'}
              </button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
            >
              关闭
            </button>
          </div>
        </div>

        {error ? (
          <p className="border-b border-border px-3 py-2 text-xs text-destructive">{error}</p>
        ) : null}

        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_420px]">
          <div
            className={`flex min-h-0 flex-col ${canvasOpen && selectedArtifact ? 'lg:border-r lg:border-border' : 'lg:col-span-2'}`}
          >
            <div ref={scrollerRef} className="min-h-0 flex-1 overflow-auto bg-muted/30 p-4">
              {loading ? <p className="text-sm text-muted-foreground">加载中...</p> : null}
              {!loading && messages.length === 0 ? (
                <p className="text-sm text-muted-foreground">还没有消息。</p>
              ) : null}
              <div className="flex flex-col gap-3">
                {messages.map((message) => (
                  <MessageBubble
                    key={message.id}
                    message={message}
                    onOpenArtifact={openArtifact}
                  />
                ))}
              </div>
            </div>

            <form onSubmit={(e) => void onSend(e)} className="border-t border-border p-3">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex overflow-hidden rounded-md border border-border text-xs">
                    {MODE_OPTIONS.map((entry) => (
                      <button
                        key={entry.value}
                        type="button"
                        title={entry.title}
                        onClick={() => setMode(entry.value)}
                        className={`px-3 py-1.5 ${
                          mode === entry.value
                            ? 'bg-primary text-primary-foreground'
                            : 'bg-background text-muted-foreground hover:bg-accent'
                        }`}
                      >
                        {entry.label}
                      </button>
                    ))}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {modeHint(mode)}
                  </span>
                </div>
                <div className="flex gap-2">
                  <textarea
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        e.currentTarget.form?.requestSubmit();
                      }
                    }}
                    placeholder="输入消息，Enter 发送，Shift+Enter 换行"
                    className="min-h-[48px] flex-1 resize-none rounded-md border border-border bg-background px-3 py-2 text-sm"
                  />
                  <button
                    type="submit"
                    disabled={!input.trim() || sending || hasRunningReply}
                    className="h-12 rounded-md bg-primary px-4 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    {sending ? '发送中' : '发送'}
                  </button>
                </div>
            </form>
          </div>

          {canvasOpen && selectedArtifact ? (
            <CanvasPanel
              artifact={selectedArtifact}
              artifacts={artifacts}
              onSelect={openArtifact}
              onClose={() => setCanvasOpen(false)}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

function modeHint(mode: RequestMode): string {
  if (mode === 'chat') return '只回复，不进入操作流程';
  if (mode === 'task') return '按任务执行并展示进度';
  if (mode === 'canvas') return '把长内容放到成果区';
  return '自动选择最佳流程';
}

function MessageBubble({
  message,
  onOpenArtifact,
}: {
  message: ChatMessage;
  onOpenArtifact: (artifact: ChatArtifact) => void;
}): JSX.Element {
  const isUser = message.role === 'user';
  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[78%] rounded-md border border-primary/30 bg-primary px-3 py-2 text-sm text-primary-foreground">
          <ReadableText text={message.content || '...'} />
        </div>
      </div>
    );
  }

  const mode = message.mode ?? message.intent?.mode ?? 'chat';
  const isTask = mode === 'task' || mode === 'task_canvas';

  return (
    <div className="flex justify-start">
      <div
        className={`max-w-[86%] rounded-md border px-3 py-2 text-sm ${
          message.status === 'error'
            ? 'border-destructive/40 bg-destructive/10 text-destructive'
            : 'border-border bg-background'
        }`}
      >
        <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <span className="rounded border border-border px-1.5 py-0.5">{modeLabel(mode)}</span>
          {message.status !== 'done' ? (
            <span className="rounded border border-border px-1.5 py-0.5">
              {statusLabel(message.status)}
            </span>
          ) : null}
          {message.intent?.confidence ? (
            <span>{Math.round(message.intent.confidence * 100)}% confidence</span>
          ) : null}
        </div>

        {isTask ? <TaskTimeline steps={message.task?.steps ?? []} /> : null}

        <ReadableText text={message.content || (message.status === 'running' ? '处理中...' : '...')} />

        {message.artifacts && message.artifacts.length > 0 ? (
          <div className="mt-3 border-t border-border pt-2">
            <p className="mb-1 text-xs font-medium">成果</p>
            <div className="flex flex-wrap gap-2">
              {message.artifacts.map((artifact) => (
                <button
                  key={artifact.id}
                  type="button"
                  onClick={() => onOpenArtifact(artifact)}
                  className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
                >
                  {artifact.title}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {message.debug_content ? (
          <details className="mt-3 border-t border-border pt-2 text-xs text-muted-foreground">
            <summary className="cursor-pointer">查看原始输出</summary>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2 font-mono">
              {message.debug_content}
            </pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function TaskTimeline({ steps }: { steps: TaskStep[] }): JSX.Element | null {
  if (steps.length === 0) return null;
  return (
    <ol className="mb-3 grid gap-1.5 border-b border-border pb-3">
      {steps.map((step) => (
        <li key={step.id} className="flex items-center gap-2 text-xs">
          <span
            className={`h-2 w-2 rounded-full ${
              step.status === 'done'
                ? 'bg-emerald-500'
                : step.status === 'running'
                  ? 'bg-primary'
                  : step.status === 'error'
                    ? 'bg-destructive'
                    : 'bg-muted-foreground/40'
            }`}
          />
          <span className="min-w-20 text-muted-foreground">{step.label}</span>
          <span>{stepStatusLabel(step.status)}</span>
        </li>
      ))}
    </ol>
  );
}

function CanvasPanel({
  artifact,
  artifacts,
  onSelect,
  onClose,
}: {
  artifact: ChatArtifact;
  artifacts: ChatArtifact[];
  onSelect: (artifact: ChatArtifact) => void;
  onClose: () => void;
}): JSX.Element {
  const text = artifact.content ?? artifact.path ?? '';

  return (
    <aside className="hidden min-h-0 flex-col bg-background lg:flex">
      <div className="flex items-start justify-between gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0">
          <h4 className="truncate text-sm font-medium">{artifact.title}</h4>
          <p className="truncate text-xs text-muted-foreground">
            Canvas · {artifact.kind}
            {artifact.path ? ` · ${artifact.path}` : ''}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            onClick={() => void navigator.clipboard?.writeText(text)}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
          >
            复制
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-accent"
          >
            关闭
          </button>
        </div>
      </div>

      {artifacts.length > 1 ? (
        <div className="flex gap-1 overflow-x-auto border-b border-border px-3 py-2">
          {artifacts.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelect(item)}
              className={`shrink-0 rounded border px-2 py-1 text-xs ${
                item.id === artifact.id
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border hover:bg-accent'
              }`}
            >
              {item.title}
            </button>
          ))}
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {text ? (
          <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-6">
            {text}
          </pre>
        ) : (
          <p className="text-sm text-muted-foreground">该成果没有可预览文本。</p>
        )}
      </div>
    </aside>
  );
}

function ReadableText({ text }: { text: string }): JSX.Element {
  return <div className="whitespace-pre-wrap break-words leading-6">{text}</div>;
}

function modeLabel(mode: string): string {
  if (mode === 'task') return 'Task';
  if (mode === 'task_canvas') return 'Task + Canvas';
  if (mode === 'canvas') return 'Canvas';
  return 'Chat';
}

function statusLabel(status: ChatMessage['status']): string {
  if (status === 'running') return '处理中';
  if (status === 'aborted') return '已停止';
  if (status === 'error') return '失败';
  return '完成';
}

function stepStatusLabel(status: TaskStep['status']): string {
  if (status === 'running') return '进行中';
  if (status === 'done') return '完成';
  if (status === 'error') return '失败';
  if (status === 'aborted') return '已停止';
  return '等待';
}
