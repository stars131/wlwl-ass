import { useMemo, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { useSkills } from '../hooks/useSkills';
import type { Skill, SkillOutcomes } from '../types';

import { ProposalsCard } from './ProposalsCard';

function formatDateTime(iso: string): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

/**
 * Bucket the success rate into a coarse traffic-light tier so the user can
 * skim down the list and spot weak skills without reading every percentage.
 * No outcomes at all → neutral tier ("untested" / "never used").
 */
function tier(outcomes: SkillOutcomes | null): 'green' | 'yellow' | 'red' | 'gray' {
  if (!outcomes || outcomes.success_rate === null) return 'gray';
  if (outcomes.success_rate >= 0.8) return 'green';
  if (outcomes.success_rate >= 0.5) return 'yellow';
  return 'red';
}

const TIER_BAR: Record<ReturnType<typeof tier>, string> = {
  green: 'bg-emerald-500',
  yellow: 'bg-amber-500',
  red: 'bg-rose-500',
  gray: 'bg-muted',
};

const TIER_TEXT: Record<ReturnType<typeof tier>, string> = {
  green: 'text-emerald-600 dark:text-emerald-400',
  yellow: 'text-amber-600 dark:text-amber-400',
  red: 'text-rose-600 dark:text-rose-400',
  gray: 'text-muted-foreground',
};

function formatRate(outcomes: SkillOutcomes | null): string {
  if (!outcomes || outcomes.success_rate === null) return '—';
  return `${Math.round(outcomes.success_rate * 100)}%`;
}

function SkillRow({ skill }: { skill: Skill }) {
  const { t } = useTranslation();
  const o = skill.outcomes;
  const colorTier = tier(o);
  const rate = formatRate(o);
  // success_rate denominator drops `exited`, so the gauge fill comes from the
  // same number to stay consistent with the displayed percentage.
  const barWidth =
    o && o.success_rate !== null ? `${Math.round(o.success_rate * 100)}%` : '0%';

  return (
    <div className="rounded-md border border-border p-3 flex flex-col gap-2 bg-card">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold truncate">{skill.title}</h3>
          <p className="text-xs text-muted-foreground font-mono truncate" title={skill.path}>
            {skill.name}
          </p>
        </div>
        <div className={`text-sm font-mono ${TIER_TEXT[colorTier]}`}>
          {rate}
          {o ? (
            <span className="ml-2 text-xs text-muted-foreground">
              {t('skills.invocations', { count: o.total })}
            </span>
          ) : (
            <span className="ml-2 text-xs text-muted-foreground">{t('skills.neverUsed')}</span>
          )}
        </div>
      </div>

      {/* Success-rate gauge — 0% width when there's no data so it visually
          collapses to a thin track. */}
      <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden" aria-hidden>
        <div
          className={`h-full ${TIER_BAR[colorTier]} transition-all`}
          style={{ width: barWidth }}
        />
      </div>

      {skill.subtitle ? (
        <p className="text-xs text-muted-foreground line-clamp-2">{skill.subtitle}</p>
      ) : null}

      {o ? (
        <div className="flex items-center gap-3 text-[11px] text-muted-foreground flex-wrap">
          <span className="text-emerald-600 dark:text-emerald-400">
            {t('skills.ok', { count: o.ok })}
          </span>
          <span className="text-rose-600 dark:text-rose-400">
            {t('skills.maxTurns', { count: o.max_turns })}
          </span>
          {o.exited > 0 ? (
            <span>{t('skills.exited', { count: o.exited })}</span>
          ) : null}
          {o.other > 0 ? <span>{t('skills.other', { count: o.other })}</span> : null}
          <span className="ml-auto" title={t('skills.lastSeenTitle')}>
            {t('skills.lastSeen')}: {formatDateTime(o.last_seen)}
          </span>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Skills tab — read-only catalogue of every SOP under memory/ plus its
 * runtime outcome stats. Lets the user see at a glance which skills the
 * agent actually exercises and which ones consistently fail.
 *
 * Polled every 15s; SOP definitions don't change often, but new turn_end
 * events should propagate to the success_rate without a manual refresh.
 */
export function SkillsPage(): JSX.Element {
  const { t } = useTranslation();
  const { data, isLoading, error, refetch, isFetching } = useSkills();
  const [filter, setFilter] = useState('');

  const skills = data?.skills ?? [];

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return skills;
    return skills.filter((s) => {
      if (s.name.toLowerCase().includes(needle)) return true;
      if (s.title.toLowerCase().includes(needle)) return true;
      if (s.subtitle.toLowerCase().includes(needle)) return true;
      return false;
    });
  }, [skills, filter]);

  const totalInvocations = useMemo(
    () => skills.reduce((sum, s) => sum + (s.outcomes?.total ?? 0), 0),
    [skills],
  );

  return (
    <div className="flex flex-col h-full p-4 gap-3">
      <header className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">{t('skills.title')}</h1>
          <p className="text-xs text-muted-foreground">{t('skills.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={t('skills.filterPlaceholder')}
            className="rounded border border-border bg-background px-2 py-1 text-xs w-44"
          />
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="px-2 py-1 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
          >
            {t('common.refresh')}
          </button>
        </div>
      </header>

      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        <span>
          {t('skills.shown', { shown: filtered.length, total: skills.length })}
        </span>
        <span>·</span>
        <span>{t('skills.totalInvocations', { count: totalInvocations })}</span>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="text-sm text-muted-foreground">{t('common.loading')}</div>
        ) : error ? (
          <div className="text-sm text-rose-600 dark:text-rose-400">
            {t('common.backendLoadFailed', { error: String(error) })}
          </div>
        ) : skills.length === 0 ? (
          <div className="text-sm text-muted-foreground">{t('skills.empty')}</div>
        ) : filtered.length === 0 ? (
          <div className="text-sm text-muted-foreground">{t('skills.noMatch')}</div>
        ) : (
          <div className="grid gap-2 grid-cols-1 lg:grid-cols-2">
            {filtered.map((s) => (
              <SkillRow key={s.name} skill={s} />
            ))}
          </div>
        )}

        <div className="mt-4">
          <ProposalsCard />
        </div>
      </div>
    </div>
  );
}
