import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getSettings, patchSettings } from '../api/settingsApi';
import type { Settings } from '../types';

const SETTINGS_KEY = ['settings'] as const;

export function useSettings() {
  return useQuery({ queryKey: SETTINGS_KEY, queryFn: getSettings });
}

export function usePatchSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<Settings>) => patchSettings(patch),
    onSuccess: (next) => {
      qc.setQueryData(SETTINGS_KEY, next);
    },
  });
}
