/**
 * Theme store + dark-mode toggle.
 *
 * Persists choice to localStorage so the chosen theme survives reloads.
 * The store applies the `dark` class to the document root, which Tailwind's
 * `darkMode: ['class']` strategy keys off (see tailwind.config.ts).
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type Theme = 'light' | 'dark' | 'system';

interface ThemeStore {
  theme: Theme;
  setTheme: (next: Theme) => void;
}

const STORAGE_KEY = 'ga-theme';

function applyDocClass(theme: Theme) {
  if (typeof document === 'undefined') return;
  const resolved =
    theme === 'system'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light'
      : theme;
  document.documentElement.classList.toggle('dark', resolved === 'dark');
}

export const useThemeStore = create<ThemeStore>()(
  persist(
    (set) => ({
      theme: 'system',
      setTheme: (next) => {
        applyDocClass(next);
        set({ theme: next });
      },
    }),
    {
      name: STORAGE_KEY,
      onRehydrateStorage: () => (state) => {
        if (state) applyDocClass(state.theme);
      },
    },
  ),
);

// Apply once on module load so the FOUC window is minimal.
if (typeof window !== 'undefined') {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { state?: { theme?: Theme } };
      if (parsed.state?.theme) applyDocClass(parsed.state.theme);
    } else {
      applyDocClass('system');
    }
  } catch {
    // ignore — falls back to default light
  }

  // React to system preference changes when in 'system' mode.
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  mq.addEventListener('change', () => {
    if (useThemeStore.getState().theme === 'system') {
      applyDocClass('system');
    }
  });
}
