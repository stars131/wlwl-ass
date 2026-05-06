import { useState } from 'react';

import { useTranslation } from 'react-i18next';

import { useDoctor } from '../hooks/useDoctor';
import type { CheckSeverity, DoctorCheck } from '../types';

const SEVERITY_DOT: Record<CheckSeverity, string> = {
  ok: 'bg-emerald-500',
  warn: 'bg-amber-500',
  fail: 'bg-rose-500',
  info: 'bg-muted-foreground/40',
};
const SEVERITY_TEXT: Record<CheckSeverity, string> = {
  ok: 'text-emerald-600 dark:text-emerald-400',
  warn: 'text-amber-600 dark:text-amber-400',
  fail: 'text-rose-600 dark:text-rose-400',
  info: 'text-muted-foreground',
};
// Display order: failures first (most urgent), then warnings, ok, info.
const SEVERITY_RANK: Record<CheckSeverity, number> = { fail: 0, warn: 1, ok: 2, info: 3 };

function CheckRow({ check }: { check: DoctorCheck }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    if (!check.fix) return;
    try {
      await navigator.clipboard.writeText(check.fix);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — silently fail; user sees the command anyway */
    }
  };
  return (
    <li className="flex flex-col gap-1 py-1.5">
      <div className="flex items-baseline gap-2">
        <span
          className={`inline-block w-2 h-2 rounded-full mt-1 ${SEVERITY_DOT[check.severity]}`}
          aria-hidden
        />
        <span className={`text-sm ${SEVERITY_TEXT[check.severity]}`}>{check.title}</span>
        <span className="ml-auto text-[10px] uppercase text-muted-foreground tracking-wider">
          {check.severity}
        </span>
      </div>
      {check.detail ? (
        <p className="text-xs text-muted-foreground pl-4">{check.detail}</p>
      ) : null}
      {check.fix ? (
        <div className="pl-4 flex items-center gap-2">
          <code className="text-xs font-mono bg-muted px-2 py-0.5 rounded flex-1 truncate"
                title={check.fix}>
            {check.fix}
          </code>
          <button
            type="button"
            onClick={onCopy}
            className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-accent shrink-0"
          >
            {copied ? t('doctor.copied') : t('doctor.copy')}
          </button>
        </div>
      ) : null}
    </li>
  );
}

/**
 * Diagnostics panel slotted into the Settings tab.
 *
 * Designed for the "I configured X but it doesn't work" flow: surfaces the
 * exact ``pip install ...`` (or other shell line) needed to fix each fail,
 * with one-click copy. Network probe is opt-in via checkbox because some
 * users hit corporate proxies that drop curl-style requests.
 */
export function DoctorPanel(): JSX.Element {
  const { t } = useTranslation();
  const [network, setNetwork] = useState(false);
  const doctor = useDoctor(network);

  const groups: Record<CheckSeverity, DoctorCheck[]> = { fail: [], warn: [], ok: [], info: [] };
  for (const c of doctor.data?.checks ?? []) groups[c.severity].push(c);

  return (
    <section className="rounded-md border border-border p-4 flex flex-col gap-3">
      <header className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          <h2 className="text-sm font-semibold">{t('doctor.title')}</h2>
          <p className="text-xs text-muted-foreground">{t('doctor.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-muted-foreground select-none">
            <input
              type="checkbox"
              checked={network}
              onChange={(e) => setNetwork(e.target.checked)}
            />
            {t('doctor.includeNetwork')}
          </label>
          <button
            type="button"
            onClick={() => doctor.refetch()}
            disabled={doctor.isFetching}
            className="px-3 py-1 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
          >
            {doctor.isFetching ? t('common.loading') : t('doctor.run')}
          </button>
        </div>
      </header>

      {doctor.error ? (
        <p className="text-xs text-rose-600 dark:text-rose-400">
          {String(doctor.error)}
        </p>
      ) : null}

      {doctor.data ? (
        <>
          <div className="flex items-center gap-3 text-xs text-muted-foreground flex-wrap">
            <span className={SEVERITY_TEXT.fail}>{t('doctor.summary.fail', { n: doctor.data.summary.fail })}</span>
            <span>·</span>
            <span className={SEVERITY_TEXT.warn}>{t('doctor.summary.warn', { n: doctor.data.summary.warn })}</span>
            <span>·</span>
            <span className={SEVERITY_TEXT.ok}>{t('doctor.summary.ok', { n: doctor.data.summary.ok })}</span>
            <span>·</span>
            <span className={SEVERITY_TEXT.info}>{t('doctor.summary.info', { n: doctor.data.summary.info })}</span>
          </div>
          <ul className="flex flex-col divide-y divide-border">
            {(['fail', 'warn', 'ok', 'info'] as CheckSeverity[]).flatMap((sev) =>
              [...groups[sev]].sort((a, b) =>
                SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || a.title.localeCompare(b.title),
              ).map((c) => <CheckRow key={c.id} check={c} />),
            )}
          </ul>
        </>
      ) : (
        <p className="text-xs text-muted-foreground">{t('doctor.notRun')}</p>
      )}
    </section>
  );
}
