import { useQuery } from '@tanstack/react-query';

import { fetchActivityRecent } from '../api/activityApi';

const ACTIVITY_KEY = ['activity', 'recent'] as const;

export function useRecentActivity(limit: number = 200) {
  return useQuery({
    queryKey: [...ACTIVITY_KEY, limit],
    queryFn: () => fetchActivityRecent(limit),
    refetchInterval: 5000,
  });
}
