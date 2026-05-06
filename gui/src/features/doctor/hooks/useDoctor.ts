import { useQuery } from '@tanstack/react-query';

import { fetchDoctor } from '../api/doctorApi';

export function useDoctor(includeNetwork: boolean = false) {
  return useQuery({
    queryKey: ['doctor', includeNetwork ? 'with-network' : 'no-network'],
    queryFn: () => fetchDoctor(includeNetwork),
    // Diagnostics don't change moment-to-moment; on-demand only.
    enabled: false,
    staleTime: 30_000,
  });
}
