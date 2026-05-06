import { useConversation } from '../state/conversation';
import { getController } from '../lib/controller';

export function Controls() {
  const paused = useConversation((s) => s.paused);
  const conn = useConversation((s) => s.connection);
  const diag = useConversation((s) => s.diagnosticsVisible);
  const toggleDiag = useConversation((s) => s.toggleDiagnostics);

  const pauseToggle = () => {
    const c = getController();
    if (paused) c.resumeUser();
    else c.pauseUser();
  };

  const endConv = () => {
    if (!confirm('结束当前对话？后台会停止聆听。')) return;
    getController().goodbye();
  };

  const forget = () => {
    if (!confirm('删除当前会话的录音和文字？无法撤销。')) return;
    getController().forgetSession();
  };

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={pauseToggle}
        title={paused ? '继续 (Space)' : '暂停 (Space)'}
        className="px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 text-sm"
      >
        {paused ? '▶  继续' : '⏸  暂停'}
      </button>
      <button
        onClick={endConv}
        title="结束对话 (Esc)"
        disabled={conn !== 'connected'}
        className="px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-sm"
      >
        ⏹  结束
      </button>
      <button
        onClick={forget}
        title="删除当前会话录音"
        className="px-3 py-1.5 rounded bg-slate-800 hover:bg-red-700 text-sm"
      >
        🗑  删除
      </button>
      <button
        onClick={toggleDiag}
        title="切换诊断信息"
        className={`px-3 py-1.5 rounded text-sm ${
          diag ? 'bg-emerald-700' : 'bg-slate-800 hover:bg-slate-700'
        }`}
      >
        {diag ? '🔍 诊断 ON' : '🔍 诊断 OFF'}
      </button>
    </div>
  );
}
