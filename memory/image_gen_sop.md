---
version: 0.1.0
tags: [image-gen, dalle, generate, illustration]
summary: 调 OpenAI 兼容的 image generation；前提是 mykey 里有 image_capable 配置。
---
# Image Generation SOP

**触发**：用户明确要求生成图片（"画一张..."、"做个 logo"、"给这段做配图"），且没有现成素材可用。

**不触发**：
- 用户想"修图"/"裁图"/"加文字" → 用 PIL `code_run` 处理，不调生成模型
- 用户想"找个图"网搜 → 用 `web_scan` + url 下载

## ⚠️ 前置：需要 image_capable 配置

工具会从 mykey 找：
1. 优先 `image_capable: True` 的 native_oai 配置
2. 兜底 host hint（`api.openai.com` / `stability` / `fal.ai` / `replicate`）

让用户在 GUI api-configs 勾"image_capable"或手编 mykey。

## 工具：`image_generate`

```
image_generate({
  "prompt": "a watercolor painting of a fox in misty forest, soft morning light",
  "output_path": "temp/img/fox.png",
  "size": "1024x1024",     # or 1792x1024 / 1024x1792 (DALL-E 3)
  "quality": "standard",   # or "hd" (DALL-E 3 only, 2x cost)
  "n": 1                   # ≥2 时写入 fox.png, fox-2.png, fox-3.png ...
})
```

返回 `{"path": "..."}`。失败时 path 以 `"Error: "` 开头。

## prompt 工程小贴士

- **风格优先**：`watercolor / oil painting / 3D render / pixel art / line drawing` 这类风格词放最前
- **主题次之**：主体 + 动作 + 场景
- **细节最后**：光线、构图、色调
- 反例：`一只猫`（太短，模型自由发挥乱画）
- 正例：`flat illustration, white background, a sleeping orange tabby cat curled up, minimalist style, single color accent`

## 何时用哪个 size / quality

| 用途 | size | quality |
|------|------|---------|
| 头像 / 图标 | 1024x1024 | standard |
| 横幅 / banner | 1792x1024 | standard |
| 海报 / 印刷 | 1024x1792 | hd |
| 多变体探索 | 1024x1024, n=4 | standard |

## 避坑

- 别用敏感内容（人物长相、品牌 logo、暴力）→ moderation 会拒
- 同一 prompt 多次调用结果不稳定（不是 bug，扩散模型本质）。要稳定就 `n=1` + 用 seed（如果模型支持）
- 大模型生成的图通常**不带透明背景**。要透明背景在 prompt 里写 `on transparent background, alpha channel` 但效果不保证；更靠谱的做法是事后用 PIL 抠 ` rembg`
