/**
 * Global keyboard shortcuts.
 *
 *   Cmd/Ctrl + 1..4   →  switch to tab N
 *   Cmd/Ctrl + ,      →  jump to Settings tab
 *   Cmd/Ctrl + /      →  show shortcut help (alert; replace with proper
 *                        modal once we have one)
 *
 * Bound at the App-shell level via useKeyboardShortcuts(setTab).
 */
import { useEffect } from 'react';

export type TabKey = 'sessions' | 'bots' | 'api-configs' | 'activity' | 'skills' | 'settings';

const NUMBER_TO_TAB: Record<string, TabKey> = {
  '1': 'sessions',
  '2': 'bots',
  '3': 'api-configs',
  '4': 'activity',
  '5': 'skills',
  '6': 'settings',
};

const HELP = `wlwl-ass 快捷键:
  Cmd/Ctrl + 1   会话
  Cmd/Ctrl + 2   Bots
  Cmd/Ctrl + 3   API 配置
  Cmd/Ctrl + 4   活动
  Cmd/Ctrl + 5   技能
  Cmd/Ctrl + 6   设置
  Cmd/Ctrl + ,   设置 (别名)
  Cmd/Ctrl + /   显示本帮助
`;

export function useKeyboardShortcuts(setTab: (tab: TabKey) => void): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;

      // Don't intercept when the user is editing text
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) {
          return;
        }
      }

      const numberTab = NUMBER_TO_TAB[e.key];
      if (numberTab) {
        e.preventDefault();
        setTab(numberTab);
        return;
      }
      if (e.key === ',') {
        e.preventDefault();
        setTab('settings');
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        // eslint-disable-next-line no-alert
        window.alert(HELP);
        return;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setTab]);
}
