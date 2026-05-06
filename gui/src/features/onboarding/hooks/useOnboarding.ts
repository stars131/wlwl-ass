import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { fetchOnboardingStatus, saveOnboarding } from '../api/onboardingApi';

const KEY = ['onboarding', 'status'] as const;

export function useOnboardingStatus() {
  return useQuery({
    queryKey: KEY,
    queryFn: fetchOnboardingStatus,
    // First-run is a one-shot — refetch every minute is plenty (covers
    // the case where the user fixes config externally while the app is
    // open and we want the wizard to dismiss itself).
    refetchInterval: 60_000,
  });
}

export function useSaveOnboarding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: saveOnboarding,
    onSuccess: (data) => qc.setQueryData(KEY, data),
  });
}
