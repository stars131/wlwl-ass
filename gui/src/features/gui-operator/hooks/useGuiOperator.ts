import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getGuiSidecarStatus,
  listGuiRuns,
  pauseGuiRun,
  resumeGuiRun,
  startGuiRun,
  stopGuiRun,
} from '../api/guiOperatorApi';
import type { StartGuiRunInput } from '../types';

const RUNS_KEY = ['gui-operator', 'runs'] as const;
const SIDECAR_KEY = ['gui-operator', 'sidecar'] as const;

export function useGuiRuns() {
  return useQuery({
    queryKey: RUNS_KEY,
    queryFn: listGuiRuns,
    refetchInterval: 1500,
  });
}

export function useGuiSidecarStatus() {
  return useQuery({
    queryKey: SIDECAR_KEY,
    queryFn: getGuiSidecarStatus,
    refetchInterval: 10000,
  });
}

export function useStartGuiRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: StartGuiRunInput) => startGuiRun(input),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: RUNS_KEY });
    },
  });
}

export function usePauseGuiRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => pauseGuiRun(runId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: RUNS_KEY });
    },
  });
}

export function useResumeGuiRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => resumeGuiRun(runId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: RUNS_KEY });
    },
  });
}

export function useStopGuiRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => stopGuiRun(runId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: RUNS_KEY });
    },
  });
}
