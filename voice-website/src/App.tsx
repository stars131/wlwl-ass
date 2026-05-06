import { useEffect } from 'react';
import { useConversation } from './state/conversation';
import { getController } from './lib/controller';
import { PermissionGate } from './components/PermissionGate';
import { StateIndicator } from './components/StateIndicator';
import { Transcript } from './components/Transcript';
import { Controls } from './components/Controls';
import { Settings } from './components/Settings';

export default function App() {
  const granted = useConversation((s) => s.micGranted);
  const lastError = useConversation((s) => s.lastError);
  const clearError = useConversation((s) => s.clearError);

  useEffect(() => {
    if (!granted) return;
    const c = getController();
    c.connect();
    return () => {
      c.disconnect();
    };
  }, [granted]);

  // Keyboard shortcuts: Space pause/resume, Esc end.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tgt = e.target as HTMLElement | null;
      if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || tgt.isContentEditable)) return;
      if (e.code === 'Space') {
        e.preventDefault();
        const s = useConversation.getState();
        if (s.paused) getController().resumeUser();
        else getController().pauseUser();
      } else if (e.code === 'Escape') {
        getController().goodbye();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div className="min-h-screen flex flex-col">
      <header className="px-6 py-4 flex items-center justify-between border-b border-slate-800">
        <div>
          <div className="text-base font-semibold">wlwl-ass · 语音控制台</div>
          <div className="text-xs text-slate-500">always listening</div>
        </div>
        <div className="flex items-center gap-2">
          <Controls />
          <Settings />
        </div>
      </header>

      <main className="flex-1 flex flex-col items-center justify-center gap-8 px-4 py-8">
        {!granted ? (
          <PermissionGate onGranted={() => undefined} />
        ) : (
          <>
            <StateIndicator />
            <Transcript />
          </>
        )}
      </main>

      {lastError && (
        <div
          role="alert"
          className="fixed bottom-4 right-4 max-w-md bg-red-900/90 border border-red-700 rounded-lg p-4 shadow-xl"
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-red-100">{lastError.code}</div>
              <div className="text-xs text-red-200 mt-1">{lastError.message}</div>
            </div>
            <button
              onClick={clearError}
              className="text-red-200 hover:text-white text-lg leading-none"
              aria-label="dismiss error"
            >
              ×
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
