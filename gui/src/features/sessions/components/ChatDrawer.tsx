import { useState } from 'react';

import { openProjectInBrowser } from '../api/sessionsApi';
import type { Project } from '../types';

interface ChatDrawerProps {
  project: Project;
  onClose: () => void;
}

/**
 * In-app chat drawer that embeds the per-project Streamlit page in an iframe.
 *
 * Why this exists: clicking ``<a target="_blank">`` inside the Tauri webview
 * does NOT open the user's default browser (no shell plugin loaded). Before
 * this drawer the "打开" button silently no-op'd. We give two paths:
 *  1. Render the Streamlit chat inline (this component) — primary UX.
 *  2. Fall back to the OS default browser via POST /api/projects/<id>/open.
 */
export function ChatDrawer({ project, onClose }: ChatDrawerProps): JSX.Element {
  const [openErr, setOpenErr] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const port = project.port;
  const url = port ? `http://127.0.0.1:${port}/` : '';

  const onOpenExternal = async () => {
    setOpening(true);
    setOpenErr(null);
    try {
      await openProjectInBrowser(project.id);
    } catch (err) {
      setOpenErr(String(err));
    } finally {
      setOpening(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-40 flex items-stretch bg-black/40"
      onClick={onClose}
    >
      <div
        className="ml-auto w-full max-w-5xl h-full flex flex-col border-l border-border bg-background"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-3 py-2 gap-2">
          <div className="min-w-0">
            <h3 className="font-medium truncate">对话：{project.name}</h3>
            <p className="text-xs text-muted-foreground truncate">{url || '(尚未分配端口)'}</p>
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              type="button"
              onClick={() => setReloadKey((k) => k + 1)}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-accent"
            >
              重载
            </button>
            <button
              type="button"
              onClick={onOpenExternal}
              disabled={opening || !project.running}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
              title="在系统默认浏览器中打开"
            >
              {opening ? '打开中…' : '系统浏览器'}
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
        {openErr ? (
          <p className="px-3 py-2 text-xs text-destructive border-b border-border">
            打开失败：{openErr}
          </p>
        ) : null}
        <div className="flex-1 min-h-0 bg-muted/30">
          {!project.running ? (
            <div className="p-6 text-sm text-muted-foreground">
              会话尚未启动。先点列表里的"启动"按钮。
            </div>
          ) : url ? (
            <iframe
              key={reloadKey}
              src={url}
              title={`chat-${project.id}`}
              className="w-full h-full border-0"
              // Streamlit needs scripts; sandbox kept permissive so its
              // own websocket / clipboard / downloads work normally.
            />
          ) : (
            <div className="p-6 text-sm text-muted-foreground">尚未分配端口。</div>
          )}
        </div>
      </div>
    </div>
  );
}
