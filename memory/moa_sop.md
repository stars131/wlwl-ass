---
version: 0.1.0
tags: [moa, mixture-of-agents, quality, ensemble]
summary: 何时把一个 prompt 同时发给多个模型再聚合，而不是只信一个模型。
---
# Mixture-of-Agents (MoA) SOP

**触发**：以下任一
1. 输出质量比延迟重要得多（例如：写一篇技术报告、设计架构、code review）
2. 用户想要"换个角度看"或者"问问别的模型怎么说"
3. 单一模型反复出错，怀疑是模型本身的盲区

**不触发**：
- 简单问答 / 日常 tool call → 用单一 session 就够
- 时间敏感任务 → MoA = 并行多模型 + 一次聚合，至少 2 倍延迟
- 涉及私密信息 → 多个模型 = 多倍数据离开你

## 与 MixinSession 的区别

| 工具 | 行为 | 用途 |
|------|------|------|
| `MixinSession` (llmcore) | 串行 failover：A 不行就 B | 容错、quota 切换 |
| `mixture_of_agents` (本工具) | 并行所有 + aggregator 整合 | 质量提升、对比基线 |

不要混用。

## 工具：`mixture_of_agents`

```
mixture_of_agents({
  "prompt": "实现一个线程安全的 LRU cache，要求 O(1) get/put",
  "member_names": ["claude-opus-4-7", "gpt-5", "gemini-2-flash"],
  "aggregator_name": "claude-opus-4-7",      # 可选，默认 = member_names[0]
  "timeout_s": 180                            # 可选
})
```

`member_names` 必须是 mykey 里的 api_config name（不是 model 名也不是 llm_no）。

返回：
```json
{
  "final": "...合成后的最终回答...",
  "member_outputs": [
    {"name":"...", "text":"...", "elapsed_ms": 4203, "error": null},
    ...
  ],
  "elapsed_ms": 5201
}
```

## 选 member 的 3 条经验

1. **质量分散**：选风格 / 训练数据不同的模型（claude + gpt + gemini > 三个 claude 变种）
2. **3 个最优**：1 个不算 mixture；4+ 收益递减、聚合上下文超长
3. **aggregator 选最强的**：聚合需要理解所有 member 输出 + 写一篇质量高于任何单个 member 的，对模型能力要求最高

## 避坑

- mixin kind 配置不能直接当 member（工具会自动解析到第一个内层成员，行为可能不直觉）
- timeout_s 内某 member 没回 → 自动降级，但 final 会标注"基于 N-1 个成员"
- 全部失败 → final 返回 `[mixture_of_agents] all members failed`，做错误处理
