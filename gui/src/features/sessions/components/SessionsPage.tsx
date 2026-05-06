import { useState } from 'react';

import { useTranslation } from 'react-i18next';

import type { Project } from '../types';
import {
  useActivateProject,
  useCreateProject,
  useProjects,
  useRenameProject,
} from '../hooks/useSessions';

import { SessionLogDrawer } from './SessionLogDrawer';
import { SessionRow } from './SessionRow';
import { ChatDrawer } from './ChatDrawer';

export function SessionsPage(): JSX.Element {
  const { t } = useTranslation();
  const projects = useProjects();
  const create = useCreateProject();
  const activate = useActivateProject();
  const rename = useRenameProject();

  const [newName, setNewName] = useState('');
  const [logProject, setLogProject] = useState<Project | null>(null);
  const [chatProject, setChatProject] = useState<Project | null>(null);

  // When the projects list refreshes, hand the up-to-date project object to
  // the chat drawer so its `running` flag and `port` stay current without
  // forcing the user to close+reopen.
  const liveChatProject = chatProject
    ? projects.data?.projects.find((p) => p.id === chatProject.id) ?? chatProject
    : null;

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    create.mutate(name, {
      onSuccess: () => setNewName(''),
    });
  };

  const onRename = (project: Project) => {
    const next = window.prompt(t('sessions.renamePrompt'), project.name);
    if (next && next.trim() && next !== project.name) {
      rename.mutate({ id: project.id, name: next.trim() });
    }
  };

  return (
    <div className="flex flex-col gap-4 p-4 max-w-3xl mx-auto">
      <header>
        <h1 className="text-xl font-semibold">{t('sessions.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('sessions.subtitle')}</p>
      </header>

      <form onSubmit={onCreate} className="flex gap-2">
        <input
          type="text"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder={t('sessions.newPlaceholder')}
          className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-sm"
        />
        <button
          type="submit"
          disabled={!newName.trim() || create.isPending}
          className="rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-sm hover:bg-primary/90 disabled:opacity-50"
        >
          {t('common.create')}
        </button>
      </form>

      {projects.isLoading ? (
        <p className="text-muted-foreground">{t('common.loading')}</p>
      ) : null}
      {projects.error ? (
        <p className="text-destructive text-sm">
          {t('common.backendLoadFailed', { error: String(projects.error) })}
        </p>
      ) : null}

      {projects.data ? (
        <ul className="flex flex-col gap-2">
          {projects.data.projects.length === 0 ? (
            <li className="text-sm text-muted-foreground italic">{t('sessions.empty')}</li>
          ) : null}
          {projects.data.projects.map((p) => (
            <li key={p.id}>
              <SessionRow
                project={p}
                isActive={p.id === projects.data.active_id}
                onActivate={(id) => activate.mutate(id)}
                onRename={onRename}
                onShowLog={setLogProject}
                onOpenChat={setChatProject}
              />
            </li>
          ))}
        </ul>
      ) : null}

      {logProject ? (
        <SessionLogDrawer
          projectId={logProject.id}
          projectName={logProject.name}
          onClose={() => setLogProject(null)}
        />
      ) : null}

      {liveChatProject ? (
        <ChatDrawer project={liveChatProject} onClose={() => setChatProject(null)} />
      ) : null}
    </div>
  );
}
