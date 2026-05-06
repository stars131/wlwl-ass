import { useState } from 'react';

import { useTranslation } from 'react-i18next';

import { useOnboardingStatus, useSaveOnboarding } from '../hooks/useOnboarding';

type Provider = 'openai' | 'anthropic';

const PROVIDER_DEFAULTS: Record<Provider, { base: string; model: string }> = {
  openai: { base: 'https://api.openai.com/v1', model: 'gpt-5.4' },
  anthropic: { base: 'https://api.anthropic.com', model: 'claude-opus-4-7' },
};

/**
 * First-run wizard modal.
 *
 * Auto-opens whenever the backend reports ``needs_setup: true`` (no usable
 * mykey + no recognized .env). Submits provider/key/base/model to the
 * onboarding endpoint, which writes them into ``<repo>/.env`` and seeds
 * ``os.environ`` so the agent can run immediately.
 *
 * Why a modal vs a dedicated tab? First-run is a *blocking* state — every
 * other tab assumes there's a working LLM. A modal pre-empts the rest of
 * the UI and goes away the moment the user is unblocked.
 *
 * The user can dismiss the modal manually (the agent still won't work,
 * but power users may be in the middle of editing .env / launcher_api_configs.json
 * and don't want us in their way).
 */
export function OnboardingModal(): JSX.Element | null {
  const { t } = useTranslation();
  const status = useOnboardingStatus();
  const save = useSaveOnboarding();
  const [provider, setProvider] = useState<Provider>('openai');
  const [apikey, setApikey] = useState('');
  const [base, setBase] = useState('');
  const [model, setModel] = useState('');
  const [dismissed, setDismissed] = useState(false);

  // Hide while the first status fetch is in flight to avoid flashing
  // the modal at every page load before we know whether it's needed.
  if (!status.data || !status.data.needs_setup || dismissed) return null;

  const defaults = PROVIDER_DEFAULTS[provider];

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apikey.trim() || save.isPending) return;
    try {
      await save.mutateAsync({
        provider,
        apikey: apikey.trim(),
        base_url: base.trim(),
        model: model.trim(),
      });
    } catch (err) {
      // Error is surfaced via save.error below; nothing else to do.
      // eslint-disable-next-line no-console
      console.error('[onboarding] save failed', err);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="onboarding-title"
    >
      <form
        onSubmit={onSubmit}
        className="bg-card border border-border rounded-lg shadow-xl w-[28rem] max-w-[92vw] p-5 flex flex-col gap-4"
      >
        <header className="flex items-baseline justify-between gap-2">
          <h2 id="onboarding-title" className="text-base font-semibold">
            {t('onboarding.title')}
          </h2>
          <button
            type="button"
            onClick={() => setDismissed(true)}
            className="text-xs text-muted-foreground hover:text-foreground"
            title={t('onboarding.dismissTitle')}
          >
            {t('onboarding.dismiss')}
          </button>
        </header>

        <p className="text-xs text-muted-foreground">{t('onboarding.subtitle')}</p>
        {status.data.reason ? (
          <p className="text-xs text-amber-600 dark:text-amber-400 font-mono break-words">
            {status.data.reason}
          </p>
        ) : null}

        <div className="flex gap-2">
          {(['openai', 'anthropic'] as const).map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setProvider(p)}
              className={`flex-1 px-3 py-1.5 rounded border text-sm ${
                provider === p
                  ? 'bg-accent text-accent-foreground border-accent'
                  : 'border-border hover:bg-muted'
              }`}
            >
              {t(`onboarding.provider.${p}`)}
            </button>
          ))}
        </div>

        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">{t('onboarding.apikey')}</span>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={apikey}
            onChange={(e) => setApikey(e.target.value)}
            placeholder={provider === 'anthropic' ? 'sk-ant-…' : 'sk-…'}
            className="rounded border border-border bg-background px-2 py-1.5 text-sm font-mono"
            required
          />
        </label>

        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            {t('onboarding.advanced')}
          </summary>
          <div className="flex flex-col gap-2 mt-2">
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">{t('onboarding.baseUrl')}</span>
              <input
                type="text"
                value={base}
                onChange={(e) => setBase(e.target.value)}
                placeholder={defaults.base}
                className="rounded border border-border bg-background px-2 py-1.5 text-xs font-mono"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">{t('onboarding.model')}</span>
              <input
                type="text"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={defaults.model}
                className="rounded border border-border bg-background px-2 py-1.5 text-xs font-mono"
              />
            </label>
          </div>
        </details>

        {save.error ? (
          <p className="text-xs text-rose-600 dark:text-rose-400 font-mono break-words">
            {String(save.error)}
          </p>
        ) : null}

        <footer className="flex items-center justify-end gap-2">
          <span className="text-[11px] text-muted-foreground mr-auto">
            {t('onboarding.savesTo')}
          </span>
          <button
            type="submit"
            disabled={!apikey.trim() || save.isPending}
            className="px-3 py-1.5 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {save.isPending ? t('common.loading') : t('onboarding.save')}
          </button>
        </footer>
      </form>
    </div>
  );
}
