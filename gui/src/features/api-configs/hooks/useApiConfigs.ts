import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  deleteProfile,
  getProfiles,
  listConfigs,
  renameProfile,
  saveConfigs,
  setActiveProfile,
  upsertProfile,
} from '../api/apiConfigsApi';
import type { ApiConfigEntry } from '../types';

const CONFIGS_KEY = ['api-configs', 'configs'] as const;
const PROFILES_KEY = ['api-configs', 'profiles'] as const;

export function useConfigs() {
  return useQuery({ queryKey: CONFIGS_KEY, queryFn: listConfigs });
}

export function useProfiles() {
  return useQuery({ queryKey: PROFILES_KEY, queryFn: getProfiles });
}

export function useSaveConfigs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (configs: ApiConfigEntry[]) => saveConfigs(configs),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONFIGS_KEY });
      void qc.invalidateQueries({ queryKey: PROFILES_KEY });
    },
  });
}

export function useSetActiveProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string | null) => setActiveProfile(name),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROFILES_KEY });
    },
  });
}

export function useUpsertProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { name: string; members: string[] }) =>
      upsertProfile(vars.name, vars.members),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROFILES_KEY });
    },
  });
}

export function useRenameProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { oldName: string; newName: string }) =>
      renameProfile(vars.oldName, vars.newName),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROFILES_KEY });
    },
  });
}

export function useDeleteProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteProfile(name),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PROFILES_KEY });
    },
  });
}
