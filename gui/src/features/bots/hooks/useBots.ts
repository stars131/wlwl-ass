import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getBotLog, installBotSdk, listBots, startBot, stopBot } from '../api/botsApi';

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
