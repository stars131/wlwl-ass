import { useEffect, useRef } from 'react';
import { useConversation } from '../state/conversation';

export function Transcript() {
  const turns = useConversation((s) => s.turns);
  const partial = useConversation((s) => s.partial);
  const diag = useConversation((s) => s.diagnosticsVisible);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: 'smooth' });
  }, [turns.length, partial?.text]);

  return (
    <div
      ref={ref}
      className="w-full max-w-2xl h-72 overflow-y-auto rounded-xl bg-slate-900/60 border border-slate-800 p-4 space-y-3"
    >
      {turns.length === 0 && !partial && (
        <div className="text-center text-slate-500 text-sm py-8">
          试试说： <span className="text-slate-300">我对三体世界说话</span>
        </div>
      )}
      {turns.map((t) => (
        <div
          key={t.turnId}
          className={`flex ${t.role === 'user' ? 'justify-end' : 'justify-start'}`}
        >
          <div
            className={[
              'rounded-2xl px-4 py-2 max-w-[80%] whitespace-pre-wrap break-words',
              t.role === 'user'
                ? 'bg-emerald-700/40 text-emerald-100'
                : t.role === 'agent'
                ? 'bg-blue-700/40 text-blue-100'
                : 'bg-slate-700/40 text-slate-300 italic text-sm',
            ].join(' ')}
          >
            <div>{t.text}</div>
            {diag && t.intent && (
              <div className="mt-1 text-[10px] uppercase tracking-wider text-slate-400">
                intent: {t.intent}
              </div>
            )}
          </div>
        </div>
      ))}
      {partial && (
        <div className="flex justify-end">
          <div className="rounded-2xl px-4 py-2 max-w-[80%] transcript-ghost">
            {partial.text}
          </div>
        </div>
      )}
    </div>
  );
}
