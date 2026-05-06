---
version: 0.1.0
tags: [voice, audio, tts, transcribe]
summary: 转写音频文件 / 生成 TTS 语音；前提是 mykey 里有 audio_capable 配置。
---
# Voice SOP

**触发**：用户给了音频文件让你听内容（语音留言/会议录音/旁白）；或者要求把回复读出来发给 IM。

**不触发**：纯文字交互 / 没有音频文件路径。

## ⚠️ 前置：需要 audio_capable 配置

工具会从 mykey 里找：
1. 优先 `audio_capable: True` 的 native_oai 配置
2. 兜底匹配 host hint（`api.openai.com` / `groq.com` / `azure.com` 等）

如果都没有，会返回 `"Error: no audio-capable native_oai config in mykey."`。

让用户在 GUI api-configs tab 编辑某个 OpenAI/Groq 配置时勾上"audio_capable"复选框；或手编 mykey 加 `'audio_capable': True`。

## 工具：`voice`

```
# 转写
voice({"action":"transcribe", "audio_path":"temp/voice/note.mp3", "language":"zh"})
# 返回：{"text": "你说的内容..."}（失败时 text 以 'Error: ' 开头）

# OpenAI TTS
voice({"action":"tts", "text":"早上好", "output_path":"temp/voice/reply.mp3", "voice":"alloy"})
# 返回：{"path": "temp/voice/reply.mp3"} 或 path 以 'Error: ' 开头

# 免费 edge-tts（可选依赖）
voice({"action":"tts_edge", "text":"早上好", "output_path":"temp/voice/reply.mp3",
       "voice":"zh-CN-XiaoxiaoNeural"})
```

`tts_edge` 不需要 API key，但要 `pip install edge-tts`。优势是无成本；缺点是中文女声风格固定，自定义少。

## 何时用哪个

| 场景 | 用 |
|------|----|
| 用户发音频问问题 | `transcribe`，再正常 agent 流程 |
| 长音频做会议纪要 | `transcribe` + 用普通文本工具摘要 |
| IM bot 回语音 | `tts` 或 `tts_edge` |
| 提示音、回执音 | `tts_edge`（省钱） |
| 多语种 / 高质量音色 | `tts`（OpenAI 多语种支持好） |

## 避坑

- 长音频（>25MB / >10min）OpenAI 会拒绝；要先用 `code_run` 切片
- transcribe 的 `language` 是 ISO 639-1 (`zh` 不是 `zh-CN`)。不传也行，模型自动检测。
- 输出路径所在目录会被自动创建（`os.makedirs(parent, exist_ok=True)`），但不要用相对路径里包含 `..`
- TTS 单次最多 ~4096 字符；长文要分段
