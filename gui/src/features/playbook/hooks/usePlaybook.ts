import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  decidePlaybookEntry,
  fetchPlaybook,
  type PlaybookEntry,
} from '../api/playbookApi';

const PLAYBOOK_KEY = ['playbook', 'entries'] as const;

export function usePlaybook(status?: PlaybookEntry['status']) {
  return useQuery({
    queryKey: [...PLAYBOOK_KEY, status ?? 'all'],
    queryFn: () => fetchPlaybook(status),
    refetchInterval: 10000,
  });
}

export function useDecidePlaybookEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      decision,
      note,
    }: {
      id: string;
      decision: 'accept' | 'reject';
      note?: string;
    }) => decidePlaybookEntry(id, decision, note),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: PLAYBOOK_KEY });
    },
  });
}
