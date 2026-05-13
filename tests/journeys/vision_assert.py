"""Visual assertion helper for the journey runner.

Two-stage check: cheap pixel diff first, then a vision-LLM tie-breaker
only when the pixels say "noticeably different" AND the journey YAML
declared an ``expected_change``. The LLM is the most expensive part of
the pipeline (≈ 1k tokens per call, $0.005-$0.02 each), so we save it
for cases where pixels can't decide on their own.

Returned shape::

    {
        "verdict": "PASS" | "FAIL" | "UNCLEAR",
        "reason": str,
        "pixel_diff_pct": float,      # 0.0-100.0
        "used_vision": bool,
    }

PASS = current matches baseline closely enough (or differs only in the
declared expected way).
FAIL = current differs from baseline in a way the journey does not
declare as expected.
UNCLEAR = vision model couldn't decide; surface for human review.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageChops


# Below this pixel-diff fraction → automatic PASS (no LLM needed).
# Anti-aliasing / cursor blink / clock-second-hand noise typically lives
# at ~0.05–0.2%. Tune up if you start eating false negatives.
PIXEL_DIFF_PASS_THRESHOLD = 0.25  # percent


def _pixel_diff_pct(current_png: str, baseline_png: str) -> float:
    """Return % of pixels that differ between the two images. Resizes the
    baseline to match the current image's size so a viewport tweak doesn't
    explode the diff. Returns 100.0 if either file can't be opened."""
    try:
        cur = Image.open(current_png).convert("RGB")
        base = Image.open(baseline_png).convert("RGB")
        if base.size != cur.size:
            base = base.resize(cur.size)
        diff = ImageChops.difference(cur, base)
        bbox = diff.getbbox()
        if bbox is None:
            return 0.0
        # Count non-zero pixels in the diff (channel-summed).
        # PIL's histogram on the diff: pixels are channel triples; we
        # collapse via convert("L") to a luminance diff, then threshold.
        gray = diff.convert("L")
        hist = gray.histogram()
        total = cur.size[0] * cur.size[1]
        # Any luminance diff > 16/255 counts (drops imperceptible noise)
        nonzero = sum(hist[17:])
        return (nonzero / total) * 100.0 if total else 0.0
    except Exception:
        return 100.0


def _vision_compare(current_png: str, baseline_png: str, expected_change: str) -> dict[str, Any]:
    """Ask the vision model whether the two screenshots are functionally
    equivalent modulo any declared expected_change. Falls back to UNCLEAR
    on any error so we don't fail a journey because the vision endpoint
    was down."""
    try:
        from tools.vision_tools import describe_image
    except Exception as exc:
        return {"verdict": "UNCLEAR", "reason": f"vision import failed: {exc}"}

    prompt = (
        "你是 GUI 视觉回归助手。我会给你看 GUI 的一张截图。"
        "请客观地描述以下要点（每点一行，≤15 字）：\n"
        "1. 顶部导航 / tab 区域的可见项\n"
        "2. 当前激活的页面\n"
        "3. 主面板的主要内容块（不需要逐字读出，只说类型/位置）\n"
        "4. 顶部条上的任何数字 / 状态徽章（如有）\n"
        "5. 任何明显的错误状态、空状态或加载状态\n"
        "不要写多余的开场白，直接列点。"
    )
    try:
        desc_cur = describe_image(current_png, prompt=prompt)
        desc_base = describe_image(baseline_png, prompt=prompt)
    except Exception as exc:
        return {"verdict": "UNCLEAR", "reason": f"vision describe failed: {exc}"}

    if desc_cur.startswith("Error:") or desc_base.startswith("Error:"):
        return {"verdict": "UNCLEAR", "reason": f"vision err: cur={desc_cur[:80]} base={desc_base[:80]}"}

    # Compare via the same vision/text backend. We feed it both
    # descriptions and the declared expected_change.
    judge_prompt = (
        "下面是同一个 GUI 在两次运行的描述。\n"
        f"【基线描述】\n{desc_base}\n\n"
        f"【当前描述】\n{desc_cur}\n\n"
        f"【已声明的预期变化】{expected_change or '(无 — 应完全等价)'}\n\n"
        "判定：两次描述在功能上是否等价？只考虑功能性差异（缺失/新增的页面元素、错误状态、"
        "数据丢失），忽略具体数字的轻微跳动（如时间、计数器）。\n"
        "输出格式严格：\n"
        "VERDICT: PASS | FAIL | UNCLEAR\n"
        "REASON: <≤40 字理由>"
    )
    # Reuse describe_image with a 1x1 transparent png as a no-op image,
    # so the call hits the same vision-capable backend without needing
    # a separate text-only client.
    try:
        from io import BytesIO
        from base64 import b64encode  # noqa: F401  (kept for clarity; unused)
        # Tiny transparent PNG; the LLM ignores it and answers from the text prompt.
        tiny = Image.new("RGB", (8, 8), "white")
        buf = BytesIO()
        tiny.save(buf, format="PNG")
        verdict_text = describe_image(buf.getvalue(), prompt=judge_prompt)
    except Exception as exc:
        return {"verdict": "UNCLEAR", "reason": f"vision judge failed: {exc}"}

    verdict = "UNCLEAR"
    reason = verdict_text[:200]
    for line in verdict_text.splitlines():
        s = line.strip()
        if s.upper().startswith("VERDICT:"):
            tag = s.split(":", 1)[1].strip().upper()
            if "PASS" in tag:
                verdict = "PASS"
            elif "FAIL" in tag:
                verdict = "FAIL"
            else:
                verdict = "UNCLEAR"
        elif s.upper().startswith("REASON:"):
            reason = s.split(":", 1)[1].strip()
    return {"verdict": verdict, "reason": reason}


def assert_visual_match(*, current_png: str, baseline_png: str,
                        expected_change: str = "") -> dict[str, Any]:
    """Two-stage compare — pixels first, vision LLM as tie-breaker."""
    cur_p = Path(current_png)
    base_p = Path(baseline_png)
    if not cur_p.exists():
        return {"verdict": "FAIL", "reason": f"missing current: {current_png}",
                "pixel_diff_pct": 100.0, "used_vision": False}
    if not base_p.exists():
        return {"verdict": "FAIL", "reason": f"missing baseline: {baseline_png}",
                "pixel_diff_pct": 100.0, "used_vision": False}

    pct = _pixel_diff_pct(current_png, baseline_png)
    if pct <= PIXEL_DIFF_PASS_THRESHOLD:
        return {"verdict": "PASS", "reason": f"pixel diff {pct:.3f}% ≤ threshold",
                "pixel_diff_pct": pct, "used_vision": False}

    # Notable pixel diff. If the journey says "no change expected" and
    # the diff is small (< 5%), still call the LLM — could be benign UI
    # noise. If diff is huge (> 30%) and no expected_change, that's a
    # confident FAIL — skip the LLM call entirely.
    if not expected_change and pct > 30.0:
        return {"verdict": "FAIL",
                "reason": f"pixel diff {pct:.2f}% with no expected_change declared",
                "pixel_diff_pct": pct, "used_vision": False}

    judged = _vision_compare(current_png, baseline_png, expected_change)
    judged["pixel_diff_pct"] = pct
    judged["used_vision"] = True
    return judged
