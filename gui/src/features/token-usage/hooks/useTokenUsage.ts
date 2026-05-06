import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { fetchTokenUsage, resetTokenUsage } from '../api/tokenUsageApi';

const KEY = ['token-usage'] as const;

export function useTokenUsage() {
  return useQuery({
    queryKey: KEY,
    queryFn: fetchTokenUsage,
    // Same cadence as activity polling: token usage piggy-backs on the
    // same "what's the agent doing right now" mental model. Backend cost
    // is trivial (a snapshot of in-memory counters).
    refetchInterval: 5000,
  });
}

export function useResetTokenUsage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: resetTokenUsage,
    onSuccess: (data) => qc.setQueryData(KEY, data),
  });
}
