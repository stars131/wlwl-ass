import { useThemeStore, type Theme } from '../store';

const ORDER: Theme[] = ['system', 'light', 'dark'];

const LABELS: Record<Theme, string> = {
  light: '☀️ 浅色',
  dark: '🌙 深色',
  system: '🖥 系统',
};

/** Cycles 系统 → 浅色 → 深色. Tooltip explains current state. */
export function ThemeToggle(): JSX.Element {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);

  const next = () => {
    const idx = ORDER.indexOf(theme);
    setTheme(ORDER[(idx + 1) % ORDER.length] ?? 'system');
  };

  return (
    <button
      type="button"
      onClick={next}
      className="px-2 py-1 text-xs rounded-md border border-border hover:bg-accent"
      title={`当前主题：${LABELS[theme]}（点击切换）`}
    >
      {LABELS[theme]}
    </button>
  );
}
