import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { cleanupDeadProcesses, killProcess, listProcesses } from '../api/processesApi';

const KEY = ['processes', 'list'] as const;

export function useProcesses() {
  return useQuery({
    queryKey: KEY,
    queryFn: listProcesses,
    refetchInterval: 5000,
  });
}

export function useKillProcess() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (target: { pid?: number; label?: string; force?: boolean }) =>
      killProcess(target),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: KEY });
    },
  });
}

export function useCleanupDeadProcesses() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => cleanupDeadProcesses(),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: KEY });
    },
  });
}
