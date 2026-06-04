import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getBotLog,
  getLlmOptions,
  installBotSdk,
  listBots,
  restartBot,
  setBotLlmBinding,
  startBot,
  stopBot,
} from '../api/botsApi';

const BOTS_KEY = ['bots', 'list'] as const;

export function useBots() {
  return useQuery({
    queryKey: BOTS_KEY,
    queryFn: listBots,
    refetchInterval: 3000,
  });
}

export function useStartBot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => startBot(key),
    // BotManager status flips immediately after spawn; refetch is enough.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: BOTS_KEY });
    },
  });
}

export function useStopBot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => stopBot(key),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: BOTS_KEY });
    },
  });
}

export function useRestartBot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => restartBot(key),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: BOTS_KEY });
    },
  });
}

export function useBotLog(key: string | null) {
  return useQuery({
    queryKey: ['bots', 'log', key],
    queryFn: () => getBotLog(key ?? ''),
    enabled: key !== null,
    refetchInterval: 5000,
  });
}

export function useInstallBotSdk() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => installBotSdk(key),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: BOTS_KEY });
    },
  });
}

export function useLlmOptions() {
  return useQuery({
    queryKey: ['bots', 'llm_options'] as const,
    queryFn: getLlmOptions,
    // Configs + profiles don't change often; only refetch on focus / mount.
    refetchInterval: false,
    staleTime: 30_000,
  });
}

export function useSetBotLlmBinding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, binding }: { key: string; binding: string }) =>
      setBotLlmBinding(key, binding),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: BOTS_KEY });
    },
  });
}
