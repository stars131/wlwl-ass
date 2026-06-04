/**
 * React Query hooks for sessions.
 *
 * Read paths use staleTime defaults from QueryClientProvider. Mutations
 * invalidate the projects list so the UI re-renders after server state
 * mutations (start/stop/create/delete/llm change).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  activateProject,
  createProject,
  deleteProject,
  getProfiles,
  listApiConfigs,
  listProjectCheckpoints,
  listProjects,
  pinProject,
  renameProject,
  setProjectAutonomous,
  setProjectLlm,
  startProject,
  stopProject,
} from '../api/sessionsApi';

const PROJECTS_KEY = ['sessions', 'projects'] as const;
const CONFIGS_KEY = ['sessions', 'configs'] as const;
const PROFILES_KEY = ['sessions', 'profiles'] as const;
const CHECKPOINTS_KEY = (id: string) => ['sessions', 'checkpoints', id] as const;

export function useProjects() {
  return useQuery({
    queryKey: PROJECTS_KEY,
    queryFn: listProjects,
    refetchInterval: 3000,
  });
}

export function useApiConfigs() {
  return useQuery({ queryKey: CONFIGS_KEY, queryFn: listApiConfigs });
}

export function useProfiles() {
  return useQuery({ queryKey: PROFILES_KEY, queryFn: getProfiles });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => createProject(name),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useStartProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: string | { id: string; resume_task_id?: string }) => {
      if (typeof vars === 'string') return startProject(vars);
      return startProject(vars.id, vars.resume_task_id ? { resume_task_id: vars.resume_task_id } : {});
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useProjectCheckpoints(id: string | null) {
  return useQuery({
    queryKey: id ? CHECKPOINTS_KEY(id) : ['sessions', 'checkpoints', 'idle'],
    queryFn: () => (id ? listProjectCheckpoints(id) : Promise.resolve({ checkpoints: [] })),
    enabled: id !== null,
  });
}

export function useStopProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => stopProject(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useRenameProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; name: string }) => renameProject(vars.id, vars.name),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useSetProjectAutonomous() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; autonomousEnabled: boolean }) =>
      setProjectAutonomous(vars.id, vars.autonomousEnabled),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function usePinProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; pinned: boolean }) => pinProject(vars.id, vars.pinned),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useActivateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => activateProject(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}

export function useSetProjectLlm() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; binding: string }) => {
      if (vars.binding.startsWith('profile:')) {
        return setProjectLlm(vars.id, { profile_name: vars.binding.slice('profile:'.length) });
      }
      if (vars.binding.startsWith('config:')) {
        return setProjectLlm(vars.id, { config_name: vars.binding.slice('config:'.length) });
      }
      return setProjectLlm(vars.id, { config_name: '', profile_name: '' });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROJECTS_KEY });
    },
  });
}
