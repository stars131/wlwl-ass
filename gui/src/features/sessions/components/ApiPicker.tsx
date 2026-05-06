/**
 * Per-session API config picker (ADR-0006).
 *
 * Lists configs whose name is part of the active profile (or all configs
 * when no profile is active). Selecting one calls PUT /api/projects/<id>/llm
 * with `{ config_name }`. The mutation invalidates the projects list so the
 * UI reflects the new pinned config name immediately.
 */
import type { Project } from '../types';
import { useApiConfigs, useProfiles, useSetProjectLlm } from '../hooks/useSessions';

interface ApiPickerProps {
  project: Project;
}

export function ApiPicker({ project }: ApiPickerProps): JSX.Element {
  const configs = useApiConfigs();
  const profiles = useProfiles();
  const setLlm = useSetProjectLlm();

  const allConfigs = configs.data ?? [];
  const activeProfile = profiles.data?.active;
  const profileMembers =
    activeProfile && profiles.data?.profiles[activeProfile]
      ? new Set(profiles.data.profiles[activeProfile])
      : null;

  const visibleConfigs = profileMembers
    ? allConfigs.filter((c) => profileMembers.has(c.name))
    : allConfigs;

  const isLoading = configs.isLoading || profiles.isLoading;
  const isPending = setLlm.isPending;
  const currentName = project.llm_config_name ?? '';

  const onChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setLlm.mutate({ id: project.id, configName: e.target.value });
  };

  return (
    <div className="flex items-center gap-2 text-sm">
      <label htmlFor={`llm-${project.id}`} className="text-muted-foreground">
        API
      </label>
      <select
        id={`llm-${project.id}`}
        value={currentName}
        onChange={onChange}
        disabled={isLoading || isPending}
        className="rounded-md border border-border bg-background px-2 py-1 text-sm disabled:opacity-50"
      >
        <option value="">(默认 / llm_no={project.llm_no})</option>
        {visibleConfigs.map((c) => (
          <option key={c.name} value={c.name}>
            {c.name}
            {c.model ? ` · ${c.model}` : ''}
          </option>
        ))}
      </select>
      {project.running ? (
        <span className="text-xs text-muted-foreground">(重启会话生效)</span>
      ) : null}
      {setLlm.isError ? (
        <span className="text-xs text-destructive">保存失败</span>
      ) : null}
    </div>
  );
}
