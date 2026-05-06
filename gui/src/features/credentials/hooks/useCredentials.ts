import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getCredentials, patchCredentials, type CredValue } from '../api/credentialsApi';

const KEY = ['credentials'] as const;

export function useCredentials() {
  return useQuery({ queryKey: KEY, queryFn: getCredentials });
}

export function usePatchCredentials() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Record<string, CredValue>) => patchCredentials(patch),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
    },
  });
}
