import { useState } from 'react';

import type { Project } from '../types';
import {
  useDeleteProject,
  usePinProject,
  useProjectCheckpoints,
  useStartProject,
  useStopProject,
} from '../hooks/useSessions';

import { ApiPicker } from './ApiPicker';

interface SessionRowProps {
  project: Project;
  isActive: boolean;
  onActivate: (id: string) => void;
  onRename: (project: Project) => void;
  onShowLog: (project: Project) => void;
  onOpenChat: (project: Project) => void;
}

export function SessionRow({
  project,
  isActive,
  onActivate,
  onRename,
  onShowLog,
  onOpenChat,
}: SessionRowProps): JSX.Element {
  const start = useStartProject();
  const stop = useStopProject();
  const pin = usePinProject();
  const del = useDeleteProject();
  const [resumePanelOpen, setResumePanelOpen] = useState(false);
  const checkpoints = useProjectCheckpoints(resumePanelOpen ? project.id : null);

  const status = project.running ? '🟢 运行中' : '⚪ 已停';

  return (
    <div
      className={`rounded-lg border p-3 ${
        isActive ? 'border-primary bg-accent/30' : 'border-border'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          className="text-left flex-1 min-w-0"
          onClick={() => onActivate(project.id)}
        >
          <div className="flex items-center gap-2">
            {project.pinned ? <span title="置顶">★</span> : null}
            <span className="font-medium truncate">{project.name}</span>
            <span className="text-xs text-muted-foreground">:{project.port ?? '—'}</span>
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {status} · id={project.id}
          </div>
          {project.last_error ? (
            <div className="text-xs text-destructive truncate mt-0.5">{project.last_error}</div>
          ) : null}
        </button>

        <div className="flex flex-col gap-1 shrink-0">
          {project.running ? (
            <button
              type="button"
              onClick={() => stop.mutate(project.id)}
              disabled={stop.isPending}
              className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
            >
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={() => start.mutate(project.id)}
              disabled={start.isPending}
              className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
            >
              启动
            </button>
          )}
          {!project.running ? (
            <button
              type="button"
              onClick={() => setResumePanelOpen((o) => !o)}
              className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
              title="从历史 checkpoint 恢复对话"
            >
              {resumePanelOpen ? '收起恢复' : '📷 恢复'}
            </button>
          ) : null}
          {project.running && project.port ? (
            <button
              type="button"
              onClick={() => onOpenChat(project)}
              className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent text-center"
            >
              打开
            </button>
          ) : null}
        </div>
      </div>

      <div className="mt-2 flex items-center justify-between gap-2 flex-wrap">
        <ApiPicker project={project} />
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => onRename(project)}
            className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
          >
            重命名
          </button>
          <button
            type="button"
            onClick={() => onShowLog(project)}
            className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
          >
            日志
          </button>
          <button
            type="button"
            onClick={() => pin.mutate({ id: project.id, pinned: !project.pinned })}
            className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
          >
            {project.pinned ? '取消置顶' : '置顶'}
          </button>
          <button
            type="button"
            onClick={() => {
              if (window.confirm(`删除 ${project.name}？`)) del.mutate(project.id);
            }}
            className="px-2 py-0.5 text-xs rounded border border-destructive/40 text-destructive hover:bg-destructive/10"
          >
            删除
          </button>
        </div>
      </div>

      {resumePanelOpen ? (
        <div className="mt-2 rounded border border-border bg-muted/30 p-2 flex flex-col gap-1.5">
          <p className="text-xs text-muted-foreground">
            从一个 checkpoint 启动会话；恢复后 agent 会看到上次的 working 记忆 + 最近上下文。
          </p>
          {checkpoints.isLoading ? (
            <p className="text-xs text-muted-foreground">加载中…</p>
          ) : checkpoints.data && checkpoints.data.checkpoints.length > 0 ? (
            <ul className="flex flex-col gap-1">
              {checkpoints.data.checkpoints.map((cp) => (
                <li
                  key={cp.task_id}
                  className="rounded border border-border bg-background p-1.5 flex items-baseline justify-between gap-2"
                >
                  <div className="min-w-0">
                    <div className="font-mono text-[11px] truncate">{cp.task_id}</div>
                    <div className="text-[10px] text-muted-foreground">
                      {cp.checkpoint_count ?? 0} 个 checkpoint · 最新 {cp.latest_saved_at ?? '—'}
                      {cp.latest_note ? ` · ${cp.latest_note}` : ''}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      start.mutate({ id: project.id, resume_task_id: cp.task_id });
                      setResumePanelOpen(false);
                    }}
                    disabled={start.isPending}
                    className="px-2 py-0.5 text-xs rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 shrink-0"
                  >
                    启动恢复
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground italic">
              此 session 还没有自动 checkpoint。先正常启动跑几轮，agent 会自动落盘。
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
