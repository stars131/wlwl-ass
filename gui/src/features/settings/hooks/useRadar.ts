import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getRadarStatus, startRadar, stopRadar } from '../api/radarApi';

const RADAR_KEY = ['radar', 'status'] as const;

// Poll every 5s while the card is mounted. radar_runner ticks every 120s
// so finer-grained polling is wasted, but 5s feels alive when the user
// clicks Start/Stop and watches the dot flip.
const POLL_MS = 5000;

export function useRadarStatus() {
  return useQuery({
    queryKey: RADAR_KEY,
    queryFn: () => getRadarStatus(20),
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: true,
  });
}

export function useStartRadar() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: startRadar,
    onSettled: () => qc.invalidateQueries({ queryKey: RADAR_KEY }),
  });
}

export function useStopRadar() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: stopRadar,
    onSettled: () => qc.invalidateQueries({ queryKey: RADAR_KEY }),
  });
}
