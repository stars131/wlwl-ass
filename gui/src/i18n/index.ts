/**
 * i18n bootstrap — react-i18next + zh/en resources.
 *
 * Initialised once via `import './i18n'` from main.tsx (side-effect import,
 * so it runs before the React tree mounts).
 *
 * Language source of truth:
 *   1. localStorage key 'ga-lang' (set by LanguageToggle / i18n store)
 *   2. browser language (navigator.language[s])
 *   3. fallback 'zh' (the historical default of the Qt launcher)
 *
 * Migration policy: components migrate to `t('key')` opportunistically.
 * Untranslated literals continue to render as Chinese — there is no global
 * cutover. Add new keys to BOTH zh.json and en.json to keep them in sync.
 */
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

import en from './locales/en.json';
import zh from './locales/zh.json';

export const SUPPORTED_LANGUAGES = ['zh', 'en'] as const;
export type Language = (typeof SUPPORTED_LANGUAGES)[number];

const STORAGE_KEY = 'ga-lang';

function detectLanguage(): Language {
  if (typeof window !== 'undefined') {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw && (SUPPORTED_LANGUAGES as readonly string[]).includes(raw)) {
        return raw as Language;
      }
    } catch {
      // ignore — fall through to browser detection
    }
    const nav = window.navigator?.language ?? '';
    if (nav.toLowerCase().startsWith('en')) return 'en';
  }
  return 'zh';
}

void i18next.use(initReactI18next).init({
  resources: {
    zh: { translation: zh },
    en: { translation: en },
  },
  lng: detectLanguage(),
  fallbackLng: 'zh',
  interpolation: { escapeValue: false },
  returnNull: false,
});

export function setLanguage(lang: Language): void {
  void i18next.changeLanguage(lang);
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      // ignore — best-effort persistence
    }
  }
}

export function getLanguage(): Language {
  const cur = i18next.language;
  return (SUPPORTED_LANGUAGES as readonly string[]).includes(cur) ? (cur as Language) : 'zh';
}

export default i18next;
