import { useEffect, useState } from 'react';

import { useQueryClient } from '@tanstack/react-query';

import { appendAuthTokenParam, getApiBase } from './env';

export type ConfigEventStatus = 'connecting' | 'open' | 'closed' | 'error';

interface ConfigEvent {
  epoch: number;
  scope: string;
  changed_at: number;
}

const CONFIG_REFRESH_KEYS: readonly unknown[][] = [
  ['api-configs'],
  ['sessions', 'configs'],
  ['sessions', 'profiles'],
  ['bots', 'list'],
  ['bots', 'llm_options'],
  ['settings'],
  ['credentials'],
  ['radar'],
  ['onboarding', 'status'],
];

export function useConfigEvents(): {
  status: ConfigEventStatus;
  lastEvent: ConfigEvent | null;
} {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<ConfigEventStatus>('connecting');
  const [lastEvent, setLastEvent] = useState<ConfigEvent | null>(null);
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    const url = appendAuthTokenParam(`${getApiBase()}/api/config/events`);
    const es = new EventSource(url);
    let lastEpoch = -1;
    let retryTimer: number | undefined;

    es.onopen = () => setStatus('open');
    es.addEventListener('config', (event) => {
      const raw = (event as MessageEvent<string>).data;
      try {
        const parsed = JSON.parse(raw) as Partial<ConfigEvent>;
        const next: ConfigEvent = {
          epoch: Number(parsed.epoch ?? 0),
          scope: String(parsed.scope ?? 'unknown'),
          changed_at: Number(parsed.changed_at ?? 0),
        };
        setLastEvent(next);
        if (next.epoch > 0 && next.epoch !== lastEpoch) {
          for (const queryKey of CONFIG_REFRESH_KEYS) {
            void queryClient.invalidateQueries({ queryKey });
          }
        }
        lastEpoch = next.epoch;
      } catch {
        // Ignore malformed frames; EventSource will stay connected.
      }
    });
    es.addEventListener('heartbeat', () => {
      if (es.readyState === EventSource.OPEN) setStatus('open');
    });
    es.addEventListener('end', () => {
      setStatus('closed');
      es.close();
      retryTimer = window.setTimeout(() => setRetryTick((n) => n + 1), 1500);
    });
    es.onerror = () => {
      setStatus('error');
      es.close();
      retryTimer = window.setTimeout(() => setRetryTick((n) => n + 1), 3000);
    };

    return () => {
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      es.close();
      setStatus('closed');
    };
  }, [queryClient, retryTick]);

  return { status, lastEvent };
}
