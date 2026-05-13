import { useState } from 'react';

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { LanguageToggle } from '@/i18n/LanguageToggle';
import { fetchHealth, fetchVersion } from '@/lib/api';
import { useKeyboardShortcuts, type TabKey } from '@/lib/keyboard';

import { ApiConfigsPage } from './features/api-configs';
import { ActivityPage } from './features/activity';
import { BotsPage } from './features/bots';
import { GuiOperatorPage } from './features/gui-operator';
import { OnboardingModal } from './features/onboarding';
import { SessionsPage } from './features/sessions';
import { SettingsPage } from './features/settings';
import { SkillsPage } from './features/skills';
import { ThemeToggle } from './features/theme';
import { TokenUsageBadge } from './features/token-usage';

const TAB_KEYS: { key: TabKey; tKey: string; hotkey: string }[] = [
  { key: 'sessions', tKey: 'app.tabs.sessions', hotkey: '⌘1' },
  { key: 'bots', tKey: 'app.tabs.bots', hotkey: '⌘2' },
  { key: 'api-configs', tKey: 'app.tabs.apiConfigs', hotkey: '⌘3' },
  { key: 'activity', tKey: 'app.tabs.activity', hotkey: '⌘4' },
  { key: 'skills', tKey: 'app.tabs.skills', hotkey: '⌘5' },
  { key: 'settings', tKey: 'app.tabs.settings', hotkey: '⌘6' },
  { key: 'gui-operator', tKey: 'app.tabs.guiOperator', hotkey: '⌘7' },
];

/**
 * Phase 1 main shell + Milestone 1 polish.
 *
 * Six tabs (sessions, bots, api-configs, activity, skills, settings) on par
 * with the Qt launcher plus the new self-evolution surfaces. Header surfaces
 * backend health + version, theme toggle, language toggle, tab keyboard
 * shortcuts (Cmd/Ctrl+1..6, Cmd/Ctrl+, , Cmd/Ctrl+/).
 */
export function App(): JSX.Element {
  const { t } = useTranslation();
  const [tab, setTab] = useState<TabKey>('sessions');
  const health = useQuery({ queryKey: ['health'], queryFn: fetchHealth, refetchInterval: 5000 });
  const version = useQuery({ queryKey: ['version'], queryFn: fetchVersion });

  useKeyboardShortcuts(setTab);

  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      <header className="border-b border-border px-4 py-3 flex items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-base font-semibold">wlwl-ass</h1>
          {version.data ? (
            <p className="text-xs text-muted-foreground truncate">
              v{version.data.version} · api {version.data.api} · py{version.data.python}
            </p>
          ) : null}
        </div>

        <nav className="flex items-center gap-1">
          {TAB_KEYS.map((entry) => {
            const label = t(entry.tKey);
            return (
              <button
                key={entry.key}
                type="button"
                onClick={() => setTab(entry.key)}
                title={`${label}  (${entry.hotkey})`}
                className={`px-3 py-1 text-sm rounded-md ${
                  tab === entry.key
                    ? 'bg-accent text-accent-foreground'
                    : 'text-muted-foreground hover:bg-muted'
                }`}
              >
                {label}
              </button>
            );
          })}
        </nav>

        <div className="flex items-center gap-2">
          <TokenUsageBadge />
          <LanguageToggle />
          <ThemeToggle />
          <span className="text-xs text-muted-foreground" title={t('app.shortcutsHint')}>
            {health.isLoading ? t('app.status.connecting') : null}
            {health.error ? t('app.status.offline') : null}
            {health.data ? t('app.status.ok') : null}
          </span>
        </div>
      </header>

      <main className="flex-1 overflow-auto">
        {tab === 'sessions' ? <SessionsPage /> : null}
        {tab === 'bots' ? <BotsPage /> : null}
        {tab === 'api-configs' ? <ApiConfigsPage /> : null}
        {tab === 'gui-operator' ? <GuiOperatorPage /> : null}
        {tab === 'activity' ? <ActivityPage /> : null}
        {tab === 'skills' ? <SkillsPage /> : null}
        {tab === 'settings' ? <SettingsPage /> : null}
      </main>
      <OnboardingModal />
    </div>
  );
}
