import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getRadarConfig,
  getRadarStatus,
  saveRadarConfig,
  startRadar,
  stopRadar,
  type RadarConfig,
} from '../api/radarApi';

const RADAR_KEY = ['radar', 'status'] as const;
const RADAR_CONFIG_KEY = ['radar', 'config'] as const;

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

export function useRadarConfig() {
  return useQuery({
    queryKey: RADAR_CONFIG_KEY,
    queryFn: getRadarConfig,
  });
}

export function useSaveRadarConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<RadarConfig>) => saveRadarConfig(patch),
    onSuccess: (data) => {
      qc.setQueryData(RADAR_CONFIG_KEY, data);
      qc.setQueryData(RADAR_KEY, data.status);
    },
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
