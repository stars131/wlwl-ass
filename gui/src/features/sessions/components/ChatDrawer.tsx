import { useEffect, useMemo, useRef, useState } from 'react';

import {
  abortProjectMessage,
  listProjectMessages,
  sendProjectMessage,
} from '../api/sessionsApi';
import type { ChatMessage, Project } from '../types';

interface ChatDrawerProps {
  project: Project;
  onClose: () => void;
}

export function ChatDrawer({ project, onClose }: ChatDrawerProps): JSX.Element {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  const hasRunningReply = useMemo(
    () => messages.some((m) => m.role === 'assistant' && m.status === 'running'),
    [messages],
  );

  const refresh = async () => {
    try {
      const data = await listProjectMessages(project.id);
      setMessages(data.messages);
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, hasRunningReply ? 800 : 2000);
    return () => window.clearInterval(timer);
  }, [project.id, hasRunningReply]);

  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, hasRunningReply]);

  const onSend = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending || !project.running || hasRunningReply) return;
    setSending(true);
    setError(null);
    try {
      const data = await sendProjectMessage(project.id, text);
      setMessages(data.messages);
      setInput('');
    } catch (err) {
      setError(String(err));
    } finally {
      setSending(false);
    }
  };

  const onAbort = async () => {
    setError(null);
    try {
      const data = await abortProjectMessage(project.id);
      setMessages(data.messages);
    } catch (err) {
      setError(String(err));
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex items-stretch bg-black/40" onClick={onClose}>
      <div
        className="ml-auto flex h-full w-full max-w-5xl flex-col border-l border-border bg-background"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
          <div className="min-w-0">
            <h3 className="truncate font-medium">对话：{project.name}</h3>
            <p className="truncate text-xs text-muted-foreground">
              {project.running ? '运行中' : '已停止'} · id={project.id}
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
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
                onClick={onAbort}
                className="rounded border border-destructive/40 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
              >
                停止回复
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

        <div ref={scrollerRef} className="min-h-0 flex-1 overflow-auto bg-muted/30 p-4">
          {loading ? <p className="text-sm text-muted-foreground">加载中...</p> : null}
          {!loading && messages.length === 0 ? (
            <p className="text-sm text-muted-foreground">还没有消息。</p>
          ) : null}
          <div className="flex flex-col gap-3">
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
          </div>
        </div>

        {!project.running ? (
          <div className="border-t border-border px-3 py-2 text-sm text-muted-foreground">
            会话已停止。先在列表里启动后再发送消息。
          </div>
        ) : (
          <form onSubmit={onSend} className="flex gap-2 border-t border-border p-3">
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
              className="min-h-[44px] flex-1 resize-none rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={!input.trim() || sending || hasRunningReply}
              className="h-11 rounded-md bg-primary px-4 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {sending ? '发送中' : '发送'}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }): JSX.Element {
  const isUser = message.role === 'user';
  const isError = message.status === 'error';
  const statusLabel =
    message.status === 'running' ? '处理中' : message.status === 'aborted' ? '已停止' : '';

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[78%] rounded-md border px-3 py-2 text-sm ${
          isUser
            ? 'border-primary/30 bg-primary text-primary-foreground'
            : isError
              ? 'border-destructive/40 bg-destructive/10 text-destructive'
              : 'border-border bg-background'
        }`}
      >
        <div className="whitespace-pre-wrap break-words">{message.content || '...'}</div>
        {statusLabel ? (
          <div className="mt-1 text-[11px] opacity-70">{statusLabel}</div>
        ) : null}
      </div>
    </div>
  );
}

