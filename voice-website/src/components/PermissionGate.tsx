import { useEffect, useState } from 'react';
import { useConversation } from '../state/conversation';
import { getController } from '../lib/controller';
import { AudioCapture } from '../lib/audio/capture';

interface Props {
  onGranted: () => void;
}

export function PermissionGate({ onGranted }: Props) {
  const granted = useConversation((s) => s.micGranted);
  const setMicGranted = useConversation((s) => s.setMicGranted);
  const [askError, setAskError] = useState<string | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const micDevice = useConversation((s) => s.micDevice);
  const setMicDevice = useConversation((s) => s.setMicDevice);

  useEffect(() => {
    if (!navigator.permissions || !navigator.permissions.query) return;
    navigator.permissions
      .query({ name: 'microphone' as PermissionName })
      .then((status) => {
        if (status.state === 'granted') setMicGranted(true);
      })
      .catch(() => undefined);
  }, [setMicGranted]);

  useEffect(() => {
    if (!granted) return;
    AudioCapture.getInputDevices().then(setDevices).catch(() => undefined);
  }, [granted]);

  if (granted) return null;

  const ask = async () => {
    setAskError(null);
    try {
      await getController().unlockAudio();
      await getController().startMic(micDevice);
      onGranted();
    } catch (err) {
      setAskError(err instanceof Error ? err.message : '权限请求失败');
    }
  };

  return (
    <div className="max-w-xl mx-auto pt-12 px-6">
      <h1 className="text-3xl font-semibold mb-3">欢迎，wlwl-ass 语音控制台</h1>
      <p className="text-slate-300 leading-relaxed mb-6">
        说话前需要授权麦克风。录音和文字保存在你自己的电脑上，30 天后自动删除。
        你也可以随时删除任何一段对话。
      </p>
      {devices.length > 0 && (
        <div className="mb-4">
          <label className="text-sm text-slate-400">输入设备</label>
          <select
            className="mt-1 w-full bg-slate-900 border border-slate-700 rounded px-3 py-2"
            value={micDevice ?? ''}
            onChange={(e) => setMicDevice(e.target.value || null)}
          >
            <option value="">系统默认</option>
            {devices.map((d) => (
              <option key={d.deviceId} value={d.deviceId}>
                {d.label || `设备 ${d.deviceId.slice(0, 6)}…`}
              </option>
            ))}
          </select>
        </div>
      )}
      <button
        onClick={ask}
        className="px-6 py-3 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium"
      >
        授权麦克风
      </button>
      {askError && (
        <div className="mt-4 text-red-400 text-sm">
          {askError}
          <details className="mt-2 text-slate-400">
            <summary>怎么恢复？</summary>
            <ul className="mt-2 ml-5 list-disc text-xs space-y-1">
              <li>Chrome / Edge：地址栏左侧锁图标 → 网站设置 → 麦克风 → 允许</li>
              <li>Safari：偏好设置 → 网站 → 麦克风</li>
              <li>iOS：设置 → Safari → 麦克风</li>
            </ul>
          </details>
        </div>
      )}
    </div>
  );
}
