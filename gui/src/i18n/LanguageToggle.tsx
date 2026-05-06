import { useTranslation } from 'react-i18next';

import { getLanguage, setLanguage, SUPPORTED_LANGUAGES, type Language } from '@/i18n';

const LABELS: Record<Language, string> = {
  zh: '中文',
  en: 'EN',
};

/**
 * Cycles language through SUPPORTED_LANGUAGES. Persisted via the i18n store.
 * Mirrors ThemeToggle's compact button style.
 */
export function LanguageToggle(): JSX.Element {
  // useTranslation subscribes the component to language changes so the label
  // refreshes when setLanguage() flips i18next's active language.
  const { i18n } = useTranslation();
  const cur = (SUPPORTED_LANGUAGES as readonly string[]).includes(i18n.language)
    ? (i18n.language as Language)
    : getLanguage();

  const next = () => {
    const idx = SUPPORTED_LANGUAGES.indexOf(cur);
    const target = SUPPORTED_LANGUAGES[(idx + 1) % SUPPORTED_LANGUAGES.length] ?? 'zh';
    setLanguage(target);
  };

  return (
    <button
      type="button"
      onClick={next}
      className="px-2 py-1 text-xs rounded-md border border-border hover:bg-accent"
      title={`${LABELS[cur]} → click to switch`}
    >
      {LABELS[cur]}
    </button>
  );
}
