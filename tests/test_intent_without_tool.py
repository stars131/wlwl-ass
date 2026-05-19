"""Tests for the "promised but didn't execute" detector in wlwl_ass.

When the agent's LLM returns a short response like "我先读取启动日志…" but
doesn't actually issue a tool_call, agent_loop would otherwise treat it as a
final response and close the turn. The detector in wlwl_ass forces the agent
to retry instead. These tests pin down the exact match surface.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


from wlwl_ass import _is_intent_without_tool, _strip_meta_blocks


# ── positive cases: should trigger a retry ────────────────────────────────

def test_intent_zh_read_log():
    """The exact transcript that surfaced the bug."""
    text = "<summary>启动失败，读日志定位</summary>我先读取启动日志，定位失败阶段和关键报错。"
    assert _is_intent_without_tool(text) is True


def test_intent_zh_let_me():
    assert _is_intent_without_tool("让我看一下这个文件的内容。") is True


def test_intent_zh_jiu_lai():
    assert _is_intent_without_tool("我这就检查 pyproject.toml 的配置") is True


def test_intent_zh_jiexialai():
    assert _is_intent_without_tool("接下来我要分析日志里的错误堆栈") is True


def test_intent_en_i_will_check():
    assert _is_intent_without_tool("I will check the log file first.") is True


def test_intent_en_let_me():
    assert _is_intent_without_tool("Let me read the project's README to get oriented.") is True


def test_intent_en_im_going_to():
    assert _is_intent_without_tool("I'm going to run the test suite.") is True


def test_intent_zh_with_thinking_block_stripped():
    """The <thinking>...</thinking> block must not save a response that's
    still just an intent statement in the visible body."""
    text = (
        "<thinking>The user wants me to investigate.</thinking>"
        "我先读取启动日志看看为什么失败。"
    )
    assert _is_intent_without_tool(text) is True


# ── negative cases: must NOT trigger ──────────────────────────────────────

def test_genuine_short_answer_not_flagged():
    """A conversational short answer (no future-tense action verb) is fine."""
    assert _is_intent_without_tool("好的，没问题。") is False
    assert _is_intent_without_tool("OK, got it.") is False


def test_conclusion_sentence_not_flagged():
    """Past-tense conclusion — model already did the work."""
    text = "已经把 cli_repl.py 写好并通过测试，可以直接运行 wlwl --version。"
    assert _is_intent_without_tool(text) is False


def test_long_answer_not_flagged():
    """The length cap protects long, genuine answers from being clobbered.

    Constructed deliberately: opens with an intent-shaped phrase that WOULD
    otherwise trigger the head regex, but the body is well over the
    300-char cap, so the detector should defer to the LLM."""
    head_intent = "我先来分析一下整体情况："
    body = (
        "首先，问题的根本原因是模型本身的 tool-use 训练不到位。"
        "在 agent_loop 的设计里，每一轮如果模型不发起 tool_call，"
        "就会被视为本轮结束。这其实是一个非常通用的约定：在 ReAct 范式下，"
        "工具调用就是行动的唯一表现形式，纯文本只算思考或最终回复。"
        "对于当前现象，从模型选型、系统提示、引擎兜底三个层面都可以缓解。"
        "模型选型层面，建议优先用 Claude 4 系或 GPT-4.1 系，"
        "因为它们经过更充分的 function-calling 训练。"
        "系统提示层面，可以在第一段就强调'动作必须落到 tool_call'，"
        "并禁用 summary 块。引擎兜底层面，可以加一个检测器，"
        "在 do_no_tool 阶段识别意图但无工具的情况并强制重试。"
        "综合权衡，先换模型成本最低，最直接。"
    )
    text = head_intent + body
    assert len(text) > 300, f"setup error: text is only {len(text)} chars"
    assert _is_intent_without_tool(text) is False


def test_advice_to_user_not_flagged():
    """When the model is advising the user ('你可以...' / 'you should...'),
    it's not making its own promise."""
    text = "你可以先运行 wlwl --version 检查安装是否成功。"
    assert _is_intent_without_tool(text) is False


def test_advice_en_not_flagged():
    text = "I'd suggest you check the log first before retrying."
    assert _is_intent_without_tool(text) is False


def test_empty_response_not_flagged():
    """Empty / blank responses are handled by a separate branch — the
    intent detector must return False so it doesn't shadow that path."""
    assert _is_intent_without_tool("") is False
    assert _is_intent_without_tool("   \n\n  ") is False


def test_summary_block_only_not_flagged():
    """If the visible body is empty after stripping summary/thinking, the
    detector should return False — let the empty-response handler take over."""
    text = "<summary>仅一个summary</summary>"
    assert _is_intent_without_tool(text) is False


# ── strip helper ──────────────────────────────────────────────────────────

def test_strip_meta_blocks_removes_summary_and_thinking():
    text = "<thinking>abc</thinking>visible<summary>def</summary>tail"
    assert _strip_meta_blocks(text) == "visibletail"


def test_strip_meta_blocks_case_insensitive():
    text = "<THINKING>x</THINKING>body<Summary>y</Summary>"
    assert _strip_meta_blocks(text) == "body"
