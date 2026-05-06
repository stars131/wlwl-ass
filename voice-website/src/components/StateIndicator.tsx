import { useConversation, type ServerState } from '../state/conversation';

const STATE_LABELS: Record<ServerState, { zh: string; en: string; cls: string }> = {
  IDLE:       { zh: '聆听中…',     en: 'Listening for wake', cls: 'state-pulse-idle' },
  ARMED:      { zh: '已唤醒，请讲', en: 'Activated, speak',   cls: 'state-pulse-armed' },
  LISTENING:  { zh: '录音中',       en: 'Recording',          cls: 'state-pulse-listening' },
  PROCESSING: { zh: '思考中…',      en: 'Thinking',           cls: 'state-pulse-processing' },
  RESPONDING: { zh: '回复中…',      en: 'Responding',         cls: 'state-pulse-responding' },
  PAUSED:     { zh: '已暂停',       en: 'Paused',             cls: 'state-pulse-idle' },
};

export function StateIndicator() {
  const state = useConversation((s) => s.serverState);
  const paused = useConversation((s) => s.paused);
  const conn = useConversation((s) => s.connection);
  const effective = paused ? 'PAUSED' : state;
  const meta = STATE_LABELS[effective];

  return (
    <div
      className="flex flex-col items-center gap-3 select-none"
      role="status"
      aria-live="polite"
      aria-label={`Voice state: ${meta.en}`}
    >
      <div
        className={`w-32 h-32 rounded-full ${meta.cls}`}
        title={`${meta.zh} · ${meta.en}`}
      />
      <div className="text-center">
        <div className="text-2xl font-semibold tracking-wide">{meta.zh}</div>
        <div className="text-xs text-slate-400 mt-1">{meta.en}</div>
        {conn !== 'connected' && (
          <div className="mt-2 text-xs text-amber-400">
            {conn === 'connecting' && '连接中…'}
            {conn === 'reconnecting' && '重连中…'}
            {conn === 'disconnected' && '未连接'}
            {conn === 'fatal' && '连接错误'}
          </div>
        )}
      </div>
    </div>
  );
}
