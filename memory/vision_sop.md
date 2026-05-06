---
version: 0.2.0
tags: [vision, ocr, image, tools]
summary: 何时用 vision 工具看图片、提文字、抽结构化字段；何时不用。
---
# Vision SOP

> ⚠️ 这是新版（基于 `tools/vision_tools.py` 的统一 `vision` 工具）。旧版 `memory/vision_api.template.py` 还在但**不再推荐**——只在 vision 工具不可用时回退。

## ⚠️ 前置规则

1. **能不用就不用**：先想"用本地 OCR (`memory/ocr_utils.py`) / 窗口标题 / 文件名 / `web_scan` 能不能搞定"。Vision 调用比文字调用贵 5-50 倍。
2. **截局部不截全屏**：截图前先用 `pygetwindow` 枚举确认窗口存在并到前台，再用 `ljqCtrl` 截窗口或局部区域。**任何场景下都禁止全屏截图**。
3. **像素上限**：vision 工具内部已经限制图片大小，但你传图前自己也要确认不是离谱大图（>5MB）。

## 工具：`vision`

三个 action：

```
vision({"action":"describe", "image_path":"temp/foo.png"})                # 自由描述
vision({"action":"describe", "image_path":"...", "prompt":"找出红色按钮的位置"})  # 带提示
vision({"action":"ocr", "image_path":"...", "lang":"zh-CN"})              # 纯文字
vision({"action":"extract", "image_path":"...", "schema":{
   "title": "string",
   "buttons": "list of button labels",
   "error_message": "if any visible error text"
}})                                                                         # JSON 抽取
```

返回值：
- `describe` / `ocr` → 字符串。**失败时以 `Error: ` 开头**，做分支判断。
- `extract` → dict。失败时 `{"error": "...", "raw": "..."}`，看 `error` 字段是否存在。

## 后端选择

工具会自动从 mykey 里挑配置：
1. 第一优先：kind=`native_claude` 的配置（claude opus/sonnet vision）
2. 兜底：kind=`native_oai` 且 model 名里含 `gpt-4o`/`gpt-5`/`o4`/`vl`/`vision` 的

如果没有任何匹配，工具会返回 `"Error: vision: no usable config found. ..."`。这时去 ConfigEditor 里加一条带视觉模型的配置。

## 典型场景

| 场景 | action |
|------|--------|
| 截了游戏画面想知道现在在哪 | describe（带具体提示） |
| OCR 识别票据 / 文档图片 | ocr |
| 从 dashboard 截图抽几个数字 | extract（带 schema） |
| 验证 UI 测试结果 | describe（"是否出现 XXX 元素"） |

## 旧 API 兼容

旧的 `memory/vision_api.py` 接口（`ask_vision(...)`）仍然可用，但只在以下场景保留：
- 你需要直接传 PIL Image 对象（vision 工具只吃路径）
- 你需要 ModelScope 后端（vision 工具暂未集成）

新代码统一用 `vision` 工具调用即可。

## 避坑

- 别在 loop 里连续 vision 调用 → 每次几秒 + 几分钱。要批处理就拼图。
- prompt 不要写"详细描述"开放式 → 你真正想知道什么就直接问什么
- extract 的 schema 越具体越准，别给"返回所有信息"
