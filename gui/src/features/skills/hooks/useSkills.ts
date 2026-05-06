import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { decideProposal, fetchProposals, fetchSkills } from '../api/skillsApi';

const SKILLS_KEY = ['skills', 'list'] as const;
const PROPOSALS_KEY = ['skills', 'proposals'] as const;

export function useSkills() {
  return useQuery({
    queryKey: SKILLS_KEY,
    queryFn: fetchSkills,
    refetchInterval: 15000,
  });
}

export function useSkillProposals() {
  return useQuery({
    queryKey: PROPOSALS_KEY,
    queryFn: fetchProposals,
    refetchInterval: 10000,
  });
}

export function useDecideProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'accept' | 'reject' }) =>
      decideProposal(id, decision),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: PROPOSALS_KEY });
    },
  });
}
