/**
 * useLogStream — subscribe to a server-sent-events log tail.
 *
 * Browser-native EventSource doesn't follow CORS preflight quirks the way
 * fetch streams do, but it also can't customise headers. Our backend
 * doesn't need any, so EventSource is the path of least resistance.
 *
 * State:
 *   - lines: rolling buffer of recent log lines (capped to maxLines)
 *   - status: 'idle' | 'connecting' | 'open' | 'closed' | 'error'
 *   - error: last error message if any
 *
 * Component contract:
 *   const { lines, status, clear, reconnect } = useLogStream(`/api/bots/${k}/log/stream`);
 *
 * Pass `null` to disable.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { appendAuthTokenParam, getApiBase } from '@/lib/env';

export type StreamStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'error';

interface UseLogStreamResult {
  lines: string[];
  status: StreamStatus;
  error: string | null;
  clear: () => void;
  reconnect: () => void;
}

const DEFAULT_MAX_LINES = 1000;

export function useLogStream(
  path: string | null,
  options: { maxLines?: number } = {},
): UseLogStreamResult {
  const maxLines = options.maxLines ?? DEFAULT_MAX_LINES;
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<StreamStatus>('idle');
  const [error, setError] = useState<string | null>(null);
  // Bumping epoch forces the connection effect to tear down + reopen.
  // The backend's SSE stream ends after `max_seconds`; without this,
  // users would have to close and reopen the panel to resume tailing.
  const [epoch, setEpoch] = useState(0);
  const esRef = useRef<EventSource | null>(null);
  const linesRef = useRef<string[]>([]);

  const append = useCallback(
    (chunk: string) => {
      const incoming = chunk.split('\n').filter((s) => s.length > 0);
      if (incoming.length === 0) return;
      const next = linesRef.current.concat(incoming);
      const trimmed = next.length > maxLines ? next.slice(next.length - maxLines) : next;
      linesRef.current = trimmed;
      setLines(trimmed);
    },
    [maxLines],
  );

  const clear = useCallback(() => {
    linesRef.current = [];
    setLines([]);
  }, []);

  const reconnect = useCallback(() => {
    setEpoch((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!path) {
      setStatus('idle');
      return;
    }
    const url = appendAuthTokenParam(`${getApiBase()}${path}`);
    setStatus('connecting');
    setError(null);
    linesRef.current = [];
    setLines([]);

    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => setStatus('open');

    // The backend sends:
    //   event: meta       — connection metadata
    //   event: append     — log data (multi-line per frame)
    //   event: heartbeat  — keepalive ping
    //   event: end        — terminal status (timeout / deleted)
    //   event: error      — fatal
    es.addEventListener('append', (ev) => {
      append((ev as MessageEvent<string>).data);
    });
    es.addEventListener('heartbeat', () => {
      // no-op; presence keeps proxies awake
    });
    es.addEventListener('end', () => {
      setStatus('closed');
      es.close();
    });
    es.addEventListener('error', (ev) => {
      const msg = (ev as MessageEvent<string>).data;
      if (msg) setError(String(msg));
    });
    es.onerror = () => {
      setStatus((prev) => (prev === 'closed' ? prev : 'error'));
      es.close();
    };

    return () => {
      es.close();
      esRef.current = null;
      setStatus('closed');
    };
  }, [path, append, epoch]);

  return { lines, status, error, clear, reconnect };
}
