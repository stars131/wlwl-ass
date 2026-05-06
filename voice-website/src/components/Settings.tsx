import { useState } from 'react';
import { useConversation } from '../state/conversation';

export function Settings() {
  const serverUrl = useConversation((s) => s.serverUrl);
  const setServerUrl = useConversation((s) => s.setServerUrl);
  const authToken = useConversation((s) => s.authToken);
  const setAuthToken = useConversation((s) => s.setAuthToken);
  const [open, setOpen] = useState(false);
  const [draftUrl, setDraftUrl] = useState(serverUrl);
  const [draftToken, setDraftToken] = useState(authToken);

  const save = () => {
    setServerUrl(draftUrl.trim());
    setAuthToken(draftToken.trim());
    setOpen(false);
    // require user to refresh; or controller can auto-reconnect
    location.reload();
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        title="设置"
        className="px-2 py-1 rounded text-sm text-slate-400 hover:text-slate-200"
      >
        ⚙
      </button>
      {open && (
        <div
          className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center"
          onClick={(e) => e.target === e.currentTarget && setOpen(false)}
        >
          <div className="bg-slate-900 rounded-xl border border-slate-700 p-6 w-[480px] max-w-[calc(100vw-2rem)]">
            <h2 className="text-lg font-semibold mb-4">设置</h2>
            <div className="space-y-3">
              <div>
                <label className="block text-sm text-slate-400 mb-1">服务器 URL</label>
                <input
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  value={draftUrl}
                  onChange={(e) => setDraftUrl(e.target.value)}
                  placeholder="ws://127.0.0.1:9700/api/voice/session"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-400 mb-1">
                  Bearer Token <span className="text-xs">(跨设备部署时填)</span>
                </label>
                <input
                  type="password"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  value={draftToken}
                  onChange={(e) => setDraftToken(e.target.value)}
                  placeholder="留空 = 不发送 Authorization 头"
                />
              </div>
            </div>
            <div className="mt-5 text-xs text-slate-500 leading-relaxed">
              <strong>数据流向：</strong>麦克风 → 你填的 WebSocket URL（默认是本机的
              wlwl-ass 后端）→ 由后端转发给 MiniMax 做 STT/TTS。录音和文字保存在
              wlwl-ass 服务器的 <code>temp/voice_sessions/</code>，30 天后自动删除。
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setOpen(false)}
                className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-sm"
              >
                取消
              </button>
              <button
                onClick={save}
                className="px-4 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-sm"
              >
                保存并重连
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
