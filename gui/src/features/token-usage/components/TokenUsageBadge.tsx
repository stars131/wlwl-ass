import { useTranslation } from 'react-i18next';

import { useResetTokenUsage, useTokenUsage } from '../hooks/useTokenUsage';

/**
 * Format a token count as a compact human-readable string. We round
 * aggressively because precision past 3 sig figs doesn't help the user —
 * the point is to see at a glance whether the agent burned 2K or 200K.
 */
function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1)}K`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}K`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function formatRate(rate: number | null): string {
  if (rate === null) return '—';
  return `${Math.round(rate * 100)}%`;
}

/**
 * Compact token-usage badge for the App header. Always visible when the
 * backend is reachable; click → reset counters (with confirm). Hover →
 * tooltip with full breakdown.
 *
 * Why no auto-hide on zero? Even an idle agent has hit *some* endpoint to
 * verify health, and seeing "↑0 ↓0" makes it obvious the counters are
 * working — better signal than disappearing widgets.
 */
export function TokenUsageBadge(): JSX.Element | null {
  const { t } = useTranslation();
  const { data, error } = useTokenUsage();
  const reset = useResetTokenUsage();

  // Hide until first successful fetch so we don't flash placeholders on
  // boot. If the endpoint hard-errors, also hide rather than render
  // garbage — the App header has its own backend-status pill.
  if (error || !data) return null;

  const { totals, cache_hit_rate } = data;
  const tooltip = [
    `${t('tokenUsage.input')}: ${totals.input.toLocaleString()}`,
    `${t('tokenUsage.output')}: ${totals.output.toLocaleString()}`,
    `${t('tokenUsage.cacheRead')}: ${totals.cache_read.toLocaleString()}`,
    `${t('tokenUsage.cacheCreation')}: ${totals.cache_creation.toLocaleString()}`,
    `${t('tokenUsage.calls')}: ${totals.calls.toLocaleString()}`,
    cache_hit_rate !== null
      ? `${t('tokenUsage.cacheHitRate')}: ${formatRate(cache_hit_rate)}`
      : '',
    `${t('tokenUsage.since')}: ${data.since}`,
    '',
    t('tokenUsage.clickToReset'),
  ]
    .filter(Boolean)
    .join('\n');

  const onClick = () => {
    if (totals.calls === 0) return; // nothing to reset
    if (window.confirm(t('tokenUsage.confirmReset'))) {
      reset.mutate();
    }
  };

  return (
    <button
      type="button"
      onClick={onClick}
      title={tooltip}
      disabled={reset.isPending}
      className="text-xs font-mono px-2 py-0.5 rounded border border-border hover:bg-muted disabled:opacity-50"
    >
      <span className="text-muted-foreground">↑</span>{' '}
      <span>{formatTokens(totals.input)}</span>
      <span className="mx-1 text-muted-foreground">↓</span>
      <span>{formatTokens(totals.output)}</span>
      {cache_hit_rate !== null && cache_hit_rate >= 0.01 ? (
        <>
          <span className="mx-1 text-muted-foreground">·</span>
          <span className="text-emerald-600 dark:text-emerald-400">
            {formatRate(cache_hit_rate)}
          </span>
        </>
      ) : null}
    </button>
  );
}
