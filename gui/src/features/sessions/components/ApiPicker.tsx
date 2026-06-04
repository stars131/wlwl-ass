/**
 * Per-session API binding picker.
 *
 * Profiles are first-class session bindings. The backend expands a selected
 * profile into member configs ordered by API category, then descending
 * priority inside that category.
 */
import type { ApiConfig, Project } from '../types';
import { useApiConfigs, useProfiles, useSetProjectLlm } from '../hooks/useSessions';

interface ApiPickerProps {
  project: Project;
}

const CATEGORY_ORDER = ['language', 'multimodal', 'voice', 'utility'];

function categoryRank(category: unknown): number {
  const idx = CATEGORY_ORDER.indexOf(String(category ?? 'language'));
  return idx < 0 ? CATEGORY_ORDER.length : idx;
}

function priority(value: unknown): number {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function sortConfigsByRuntimePriority(configs: ApiConfig[]): ApiConfig[] {
  return [...configs].sort((a, b) => {
    const categoryDelta = categoryRank(a.category) - categoryRank(b.category);
    if (categoryDelta !== 0) return categoryDelta;
    return priority(b.priority) - priority(a.priority);
  });
}

export function ApiPicker({ project }: ApiPickerProps): JSX.Element {
  const configs = useApiConfigs();
  const profiles = useProfiles();
  const setLlm = useSetProjectLlm();

  const allConfigs = configs.data ?? [];
  const allProfiles = Object.entries(profiles.data?.profiles ?? {}).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  const configsByName = new Map(allConfigs.map((config) => [config.name, config]));

  const isLoading = configs.isLoading || profiles.isLoading;
  const isPending = setLlm.isPending;
  const currentBinding = project.llm_profile_name
    ? `profile:${project.llm_profile_name}`
    : project.llm_config_name
      ? `config:${project.llm_config_name}`
      : '';

  const onChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setLlm.mutate({ id: project.id, binding: e.target.value });
  };

  const describeProfile = (members: string[]): string => {
    const orderedMembers = sortConfigsByRuntimePriority(
      members
        .map((name) => configsByName.get(name))
        .filter((config): config is ApiConfig => Boolean(config)),
    ).map((config) => `${config.name}(P${priority(config.priority)})`);
    return orderedMembers.length ? orderedMembers.join(' > ') : '未配置成员';
  };

  return (
    <div className="flex items-center gap-2 text-sm">
      <label htmlFor={`llm-${project.id}`} className="text-muted-foreground">
        API
      </label>
      <select
        id={`llm-${project.id}`}
        value={currentBinding}
        onChange={onChange}
        disabled={isLoading || isPending}
        className="min-w-[16rem] rounded-md border border-border bg-background px-2 py-1 text-sm disabled:opacity-50"
      >
        <option value="">默认 / llm_no={project.llm_no}</option>
        {allProfiles.length ? (
          <optgroup label="Profiles">
            {allProfiles.map(([name, members]) => (
              <option key={name} value={`profile:${name}`}>
                Profile: {name} - {describeProfile(members)}
              </option>
            ))}
          </optgroup>
        ) : null}
        <optgroup label="单个 API">
          {allConfigs.map((config) => (
            <option key={config.name} value={`config:${config.name}`}>
              {config.name}
              {config.category ? ` / ${config.category}` : ''}
              {` / P${priority(config.priority)}`}
              {config.model ? ` / ${config.model}` : ''}
            </option>
          ))}
        </optgroup>
      </select>
      {project.running ? <span className="text-xs text-muted-foreground">(重启会话生效)</span> : null}
      {setLlm.isError ? <span className="text-xs text-destructive">保存失败</span> : null}
    </div>
  );
}
