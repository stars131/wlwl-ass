"""Desktop GUI operator primitives.

This module is the first small bridge from wlwl-ass's tool loop to a
UI-TARS-style GUI grounding workflow. It deliberately stays Python-native:

* ``observe_desktop`` captures a screenshot into ``temp/gui_runs``.
* ``parse_action`` parses UI-TARS-style actions such as
  ``click(start_box='[100, 200, 120, 220]')``.
* ``execute_desktop_action`` performs the parsed mouse/keyboard action.

The parser is side-effect free and covered by tests. Real input actions are
high risk and are exposed through the normal wlwl-ass permission layer.
"""
from __future__ import annotations

import base64
import contextlib
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_FACTORS = (1000.0, 1000.0)
TERMINAL_ACTIONS = {"finished", "call_user", "error_env", "user_stop"}
NO_ACTION_RETRY_SUFFIX = (
    "\nReply with exactly one Action call on a single line. "
    "Do not include Thought, explanations, or markdown. "
    "If the screen does not allow progress, return finished() or call_user(content='...')."
)

UI_TARS_ACTION_PROMPT = """You are controlling a desktop GUI from a screenshot.

Task: {instruction}

Return exactly one next action call and no markdown. Use normalized screenshot
coordinates in a 0-1000 box. Prefer these action forms:
- click(start_box='[x1,y1,x2,y2]')
- type(content='text')
- hotkey(key='ctrl+l')
- scroll(start_box='[x1,y1,x2,y2]', direction='down', clicks='5')
- drag(start_box='[x1,y1,x2,y2]', end_box='[x1,y1,x2,y2]')
- wait(seconds='1')
- finished()
- call_user(content='question for the user')
- error_env(content='why the UI cannot complete the task')
"""


@dataclass
class ParsedAction:
    action_type: str
    action_inputs: dict[str, Any]
    start_coords: tuple[int, int] | None = None
    end_coords: tuple[int, int] | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_screenshot_path() -> Path:
    run_dir = _project_root() / "temp" / "gui_runs" / time.strftime("%Y%m%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / f"screenshot-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.png"


def _screen_metrics() -> dict[str, Any]:
    if sys.platform == "win32":
        try:
            import ctypes

            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
            user32 = ctypes.windll.user32
            logical_width = int(user32.GetSystemMetrics(0))
            logical_height = int(user32.GetSystemMetrics(1))
            # Virtual screen spans ALL monitors. Origin can be negative when
            # a secondary monitor sits to the left/above the primary. We
            # surface these so multi-monitor screenshots translate correctly
            # to ``SetCursorPos`` coordinates (which use virtual-screen space).
            virtual_x = int(user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
            virtual_y = int(user32.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
            virtual_w = int(user32.GetSystemMetrics(78))   # SM_CXVIRTUALSCREEN
            virtual_h = int(user32.GetSystemMetrics(79))   # SM_CYVIRTUALSCREEN
            return {
                "logical_width": logical_width,
                "logical_height": logical_height,
                "virtual_x": virtual_x,
                "virtual_y": virtual_y,
                "virtual_width": virtual_w,
                "virtual_height": virtual_h,
                "platform": "win32",
            }
        except Exception:
            pass
    return {"platform": sys.platform}


def _gui_runs_root() -> Path:
    return (_project_root() / "temp" / "gui_runs").resolve()


def _resolve_screenshot_path(output_path: str | None) -> tuple[Path | None, str | None]:
    """Resolve and validate ``output_path`` for ``observe_desktop``.

    Returns ``(path, error_detail)``. Paths must live under ``temp/gui_runs/``
    to avoid arbitrary write via LLM-supplied parameters.
    """
    if output_path is None:
        return _default_screenshot_path(), None
    raw = Path(output_path).expanduser()
    candidate = raw if raw.is_absolute() else (_project_root() / raw)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError) as exc:
        return None, f"cannot resolve output_path: {exc}"
    allowed = _gui_runs_root()
    try:
        resolved.relative_to(allowed)
    except ValueError:
        return None, f"output_path must be inside {allowed}"
    return resolved, None


def observe_desktop(
    *,
    output_path: str | None = None,
    include_base64: bool = False,
    all_screens: bool = False,
    record: bool = True,
) -> dict[str, Any]:
    """Capture the current desktop screenshot.

    Pillow is intentionally optional. If it is missing, the caller gets an
    error string instead of an exception that would abort the agent loop.
    """
    path, error = _resolve_screenshot_path(output_path)
    if error is not None or path is None:
        return {
            "status": "error",
            "error": "path_not_allowed",
            "detail": error or "invalid output_path",
        }
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from PIL import ImageGrab
    except Exception as exc:
        return {
            "status": "error",
            "error": "missing_dependency",
            "detail": f"Pillow is required for desktop screenshots: {exc}",
            "install_hint": 'pip install "Pillow>=10"',
        }

    try:
        try:
            image = ImageGrab.grab(all_screens=all_screens)
            image.save(path)
        except Exception as pillow_exc:
            image = _screenshot_with_pyautogui(path)
            if image is None:
                raise pillow_exc
    except Exception as exc:
        return {
            "status": "error",
            "error": "screenshot_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    metrics = _screen_metrics()
    width, height = image.size
    logical_width = metrics.get("logical_width")
    scale_factor = 1.0
    if isinstance(logical_width, int) and logical_width > 0 and not all_screens:
        scale_factor = round(width / logical_width, 4)

    result: dict[str, Any] = {
        "status": "success",
        "target": "desktop",
        "path": str(path),
        "width": width,
        "height": height,
        "logical_width": metrics.get("logical_width", width),
        "logical_height": metrics.get("logical_height", height),
        "scale_factor": scale_factor,
        "all_screens": bool(all_screens),
        "virtual_x": metrics.get("virtual_x", 0),
        "virtual_y": metrics.get("virtual_y", 0),
        "virtual_width": metrics.get("virtual_width", width),
        "virtual_height": metrics.get("virtual_height", height),
        "mime": "image/png",
    }
    if include_base64:
        with open(path, "rb") as f:
            result["base64"] = base64.b64encode(f.read()).decode("ascii")

    if record:
        _record_gui_step({
            "target": "desktop",
            "action": "observe",
            "status": "success",
            "screenshot_path": str(path),
            "width": width,
            "height": height,
            "scale_factor": scale_factor,
        })
    return result


def _screenshot_with_pyautogui(path: Path) -> Any | None:
    try:
        import pyautogui
    except Exception:
        return None
    image = pyautogui.screenshot()
    image.save(path)
    return image


def parse_prediction(prediction: str) -> list[str]:
    """Extract one or more action calls from raw VLM output."""
    text = (prediction or "").strip()
    if not text:
        return []
    marker = re.search(r"(?:^|\n)\s*Action[:：]\s*", text)
    if marker:
        text = text[marker.end():]
        text = re.sub(r"(?:^|\n)\s*Action[:：]\s*", "\n\n", text).strip()
    # UI-TARS commonly separates multiple actions by blank lines.
    candidates = [
        _strip_code_fence(p.strip())
        for p in re.split(r"\n\s*\n", text)
        if p.strip()
    ]
    if len(candidates) == 1:
        # Fallback: one action per non-empty line.
        lines = [line.strip() for line in candidates[0].splitlines() if line.strip()]
        if len(lines) > 1 and all("(" in line and line.endswith(")") for line in lines):
            candidates = lines
    candidates = [c for c in candidates if _looks_like_action_call(c)]
    return candidates


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _looks_like_action_call(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z_]\w*\(.*\)$", text.strip(), flags=re.DOTALL))


def build_action_prompt(instruction: str, *, step: int | None = None) -> str:
    prompt = UI_TARS_ACTION_PROMPT.format(instruction=(instruction or "").strip())
    if step is not None:
        prompt += f"\nCurrent step: {step}\n"
    return prompt


def parse_action(
    action_text: str,
    *,
    screen_width: int,
    screen_height: int,
    scale_factor: float = 1.0,
    factors: tuple[float, float] = DEFAULT_FACTORS,
    origin: tuple[int, int] = (0, 0),
) -> ParsedAction:
    action_type, inputs = _parse_function_call(action_text)
    start_coords = _coords_from_inputs(
        inputs,
        "start_box",
        screen_width=screen_width,
        screen_height=screen_height,
        scale_factor=scale_factor,
        factors=factors,
        origin=origin,
    )
    end_coords = _coords_from_inputs(
        inputs,
        "end_box",
        screen_width=screen_width,
        screen_height=screen_height,
        scale_factor=scale_factor,
        factors=factors,
        origin=origin,
    )
    if start_coords is None and "x" in inputs and "y" in inputs:
        try:
            start_coords = (int(float(inputs["x"])), int(float(inputs["y"])))
        except (TypeError, ValueError):
            start_coords = None
    if end_coords is None and "end_x" in inputs and "end_y" in inputs:
        try:
            end_coords = (int(float(inputs["end_x"])), int(float(inputs["end_y"])))
        except (TypeError, ValueError):
            end_coords = None
    return ParsedAction(
        action_type=action_type,
        action_inputs=inputs,
        start_coords=start_coords,
        end_coords=end_coords,
    )


def parse_actions(
    prediction: str,
    *,
    screen_width: int,
    screen_height: int,
    scale_factor: float = 1.0,
    factors: tuple[float, float] = DEFAULT_FACTORS,
    origin: tuple[int, int] = (0, 0),
) -> list[ParsedAction]:
    return [
        parse_action(
            action,
            screen_width=screen_width,
            screen_height=screen_height,
            scale_factor=scale_factor,
            factors=factors,
            origin=origin,
        )
        for action in parse_prediction(prediction)
    ]


def parsed_action_to_dict(parsed: ParsedAction) -> dict[str, Any]:
    return _parsed_as_dict(parsed)


def execute_desktop_action(
    action_text: str,
    *,
    screen_width: int | None = None,
    screen_height: int | None = None,
    scale_factor: float = 1.0,
    dry_run: bool = True,
    record: bool = True,
    origin: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    metrics = _screen_metrics()
    screen_width = int(screen_width or metrics.get("logical_width") or 0)
    screen_height = int(screen_height or metrics.get("logical_height") or 0)
    if screen_width <= 0 or screen_height <= 0:
        return {
            "status": "error",
            "error": "missing_screen_size",
            "detail": "screen_width and screen_height are required on this platform",
        }

    parsed = parse_action(
        action_text,
        screen_width=screen_width,
        screen_height=screen_height,
        scale_factor=scale_factor,
        origin=origin,
    )
    parsed_dict = _parsed_as_dict(parsed)
    if dry_run:
        if record:
            _record_gui_step({
                "target": "desktop",
                "action": parsed.action_type,
                "status": "dry_run",
                "parsed": parsed_dict,
            })
        return {"status": "dry_run", "target": "desktop", "parsed": parsed_dict}

    try:
        result = _execute_desktop_parsed(parsed)
    except Exception as exc:
        result = {
            "status": "error",
            "error": "execute_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "parsed": parsed_dict,
        }

    if record:
        _record_gui_step({
            "target": "desktop",
            "action": parsed.action_type,
            "status": result.get("status", "unknown"),
            "parsed": parsed_dict,
        })
    return result


def run_visual_task(
    instruction: str,
    *,
    run_id: str | None = None,
    max_loop: int = 5,
    loop_wait: float = 1.0,
    dry_run: bool = True,
    include_base64: bool = False,
    all_screens: bool = False,
    backend: str = "auto",
    should_stop: Callable[[], bool] | None = None,
    wait_if_paused: Callable[[], bool] | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a small screenshot -> VLM -> action loop.

    The loop intentionally reuses ``tools.vision_tools`` so it follows the
    same OpenAI-compatible / Claude vision configuration as the existing
    ``vision`` tool. ``dry_run`` defaults to True so the first use generates
    an auditable plan and trajectory without moving the real desktop.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return {"status": "error", "error": "missing_instruction", "steps": []}
    max_loop = max(1, int(max_loop or 1))
    loop_wait = max(0.0, float(loop_wait or 0.0))
    run_id = run_id or (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_dir = _project_root() / "temp" / "gui_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    status = "max_loop"
    for idx in range(1, max_loop + 1):
        if should_stop and should_stop():
            status = "stopped"
            break
        pause_started = time.time()
        if wait_if_paused and not wait_if_paused():
            status = "stopped"
            break
        started = time.time()
        observed = observe_desktop(
            output_path=str(run_dir / f"step-{idx:03d}.png"),
            include_base64=include_base64,
            all_screens=all_screens,
            record=False,
        )
        if observed.get("status") != "success":
            step = {
                "step": idx,
                "status": "error",
                "error": observed.get("error"),
                "detail": observed.get("detail"),
                "observe": observed,
                "elapsed_s": round(time.time() - started, 3),
            }
            steps.append(step)
            _notify_step(on_step, step)
            status = "error"
            _record_gui_step({
                "run_id": run_id,
                "step": idx,
                "target": "desktop",
                "action": "run_observe",
                "status": "error",
                "screenshot_path": observed.get("path", ""),
                "error": observed.get("error", ""),
                "detail": observed.get("detail", ""),
                "paused_s": round(started - pause_started, 3),
                "elapsed_s": step["elapsed_s"],
            })
            break

        prompt = build_action_prompt(instruction, step=idx)
        prediction = _predict_action_with_vision(
            observed["path"],
            prompt=prompt,
            backend=backend,
        )
        if prediction.startswith("Error:"):
            step = {
                "step": idx,
                "status": "error",
                "error": "vision_failed",
                "prediction": prediction,
                "observe": observed,
                "elapsed_s": round(time.time() - started, 3),
            }
            steps.append(step)
            _notify_step(on_step, step)
            status = "error"
            _record_gui_step({
                "run_id": run_id,
                "step": idx,
                "target": "desktop",
                "action": "run_predict",
                "status": "error",
                "screenshot_path": observed.get("path", ""),
                "width": observed.get("width"),
                "height": observed.get("height"),
                "scale_factor": observed.get("scale_factor"),
                "prediction": prediction[:500],
                "paused_s": round(started - pause_started, 3),
                "elapsed_s": step["elapsed_s"],
            })
            break

        try:
            # For multi-monitor (``all_screens=True``) captures, the screenshot
            # IS the virtual desktop. Coordinates must be in virtual-screen
            # space (which ``SetCursorPos`` accepts and ``pyautogui`` does
            # on Windows). Using the single-screen ``logical_width`` here
            # would project a 2-monitor click onto the primary monitor only.
            if observed.get("all_screens"):
                action_screen_w = int(observed.get("virtual_width") or observed["width"])
                action_screen_h = int(observed.get("virtual_height") or observed["height"])
                action_scale = 1.0
                action_origin = (
                    int(observed.get("virtual_x") or 0),
                    int(observed.get("virtual_y") or 0),
                )
            else:
                action_screen_w = int(observed.get("logical_width") or observed["width"])
                action_screen_h = int(observed.get("logical_height") or observed["height"])
                action_scale = float(observed.get("scale_factor") or 1.0)
                action_origin = (0, 0)
            parsed = parse_actions(
                prediction,
                screen_width=action_screen_w,
                screen_height=action_screen_h,
                scale_factor=action_scale,
                origin=action_origin,
            )
        except Exception as exc:
            step = {
                "step": idx,
                "status": "error",
                "error": "parse_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "prediction": prediction,
                "observe": observed,
                "elapsed_s": round(time.time() - started, 3),
            }
            steps.append(step)
            _notify_step(on_step, step)
            status = "error"
            _record_gui_step({
                "run_id": run_id,
                "step": idx,
                "target": "desktop",
                "action": "run_parse",
                "status": "error",
                "screenshot_path": observed.get("path", ""),
                "width": observed.get("width"),
                "height": observed.get("height"),
                "scale_factor": observed.get("scale_factor"),
                "prediction": prediction[:500],
                "detail": step["detail"],
                "paused_s": round(started - pause_started, 3),
                "elapsed_s": step["elapsed_s"],
            })
            break

        if not parsed:
            # The model produced thought without an Action line. Try once more
            # with a stricter prompt before declaring failure — VLMs commonly
            # need the explicit nudge instead of bailing the whole run.
            stricter_prompt = build_action_prompt(instruction, step=idx) + NO_ACTION_RETRY_SUFFIX
            retry_prediction = _predict_action_with_vision(
                observed["path"],
                prompt=stricter_prompt,
                backend=backend,
            )
            if not retry_prediction.startswith("Error:"):
                try:
                    retry_parsed = parse_actions(
                        retry_prediction,
                        screen_width=action_screen_w,
                        screen_height=action_screen_h,
                        scale_factor=action_scale,
                        origin=action_origin,
                    )
                except Exception:
                    retry_parsed = []
                if retry_parsed:
                    prediction = retry_prediction
                    parsed = retry_parsed

        if not parsed:
            step = {
                "step": idx,
                "status": "error",
                "error": "no_action",
                "prediction": prediction,
                "observe": observed,
                "elapsed_s": round(time.time() - started, 3),
            }
            steps.append(step)
            _notify_step(on_step, step)
            status = "error"
            _record_gui_step({
                "run_id": run_id,
                "step": idx,
                "target": "desktop",
                "action": "run_parse",
                "status": "no_action",
                "screenshot_path": observed.get("path", ""),
                "width": observed.get("width"),
                "height": observed.get("height"),
                "scale_factor": observed.get("scale_factor"),
                "prediction": prediction[:500],
                "paused_s": round(started - pause_started, 3),
                "elapsed_s": step["elapsed_s"],
            })
            break

        first = parsed[0]
        action_text = parse_prediction(prediction)[0]
        if first.action_type.lower() in TERMINAL_ACTIONS:
            step = {
                "step": idx,
                "status": "finished" if first.action_type.lower() == "finished" else first.action_type.lower(),
                "prediction": prediction,
                "action_text": action_text,
                "parsed": _parsed_as_dict(first),
                "observe": observed,
                "elapsed_s": round(time.time() - started, 3),
            }
            steps.append(step)
            _notify_step(on_step, step)
            status = step["status"]
            _record_gui_step({
                "run_id": run_id,
                "step": idx,
                "target": "desktop",
                "action": first.action_type,
                "status": status,
                "screenshot_path": observed.get("path", ""),
                "width": observed.get("width"),
                "height": observed.get("height"),
                "scale_factor": observed.get("scale_factor"),
                "parsed": step["parsed"],
                "prediction": prediction[:500],
                "paused_s": round(started - pause_started, 3),
                "elapsed_s": step["elapsed_s"],
            })
            break

        result = execute_desktop_action(
            action_text,
            screen_width=action_screen_w,
            screen_height=action_screen_h,
            scale_factor=action_scale,
            dry_run=dry_run,
            record=False,
            origin=action_origin,
        )
        step = {
            "step": idx,
            "status": result.get("status", "unknown"),
            "prediction": prediction,
            "action_text": action_text,
            "parsed": result.get("parsed") or _parsed_as_dict(first),
            "observe": observed,
            "result": result,
            "elapsed_s": round(time.time() - started, 3),
        }
        steps.append(step)
        _notify_step(on_step, step)
        _record_gui_step({
            "run_id": run_id,
            "step": idx,
            "target": "desktop",
            "action": first.action_type,
            "status": step["status"],
            "screenshot_path": observed.get("path", ""),
            "width": observed.get("width"),
            "height": observed.get("height"),
            "scale_factor": observed.get("scale_factor"),
            "parsed": step["parsed"],
            "prediction": prediction[:500],
            "paused_s": round(started - pause_started, 3),
            "elapsed_s": step["elapsed_s"],
        })
        if result.get("status") == "error":
            status = "error"
            break
        if dry_run:
            status = "dry_run"
            break
        if loop_wait and not _controlled_sleep(loop_wait, should_stop, wait_if_paused):
            status = "stopped"
            break

    return {
        "status": status,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "dry_run": dry_run,
        "steps": steps,
    }


def _notify_step(callback: Callable[[dict[str, Any]], None] | None, step: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(step)
    except Exception:
        pass


def _controlled_sleep(
    seconds: float,
    should_stop: Callable[[], bool] | None,
    wait_if_paused: Callable[[], bool] | None,
) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if should_stop and should_stop():
            return False
        if wait_if_paused and not wait_if_paused():
            return False
        time.sleep(min(0.2, max(0.0, deadline - time.time())))
    return True


def _parse_function_call(action_text: str) -> tuple[str, dict[str, str]]:
    text = (action_text or "").strip()
    text = text.replace("<|box_start|>", "").replace("<|box_end|>", "")
    text = (
        text.replace("start_point=", "start_box=")
        .replace("end_point=", "end_box=")
    )
    # Bare ``point=`` is the UI-TARS alias for ``start_box=``. Only rewrite
    # standalone occurrences — anything prefixed by an identifier char (e.g.
    # ``screen_point=``, ``anchor_point=``) is a different key the model meant.
    text = re.sub(r"(?<![A-Za-z_])point=", "start_box=", text)
    match = re.match(r"^([A-Za-z_]\w*)\((.*)\)$", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"not an action call: {action_text!r}")
    name, args_blob = match.group(1), match.group(2).strip()
    if not args_blob:
        return name, {}
    inputs: dict[str, str] = {}
    for chunk in _split_args(args_blob):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        inputs[key.strip()] = _unquote(value.strip())
    if "start_box" not in inputs:
        for alias in ("bbox", "box", "start_bbox"):
            if alias in inputs:
                inputs["start_box"] = inputs[alias]
                break
    if "end_box" not in inputs and "end_bbox" in inputs:
        inputs["end_box"] = inputs["end_bbox"]
    return name, inputs


def _split_args(args_blob: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    escape = False
    for ch in args_blob:
        if escape:
            current.append(ch)
            escape = False
            continue
        if ch == "\\":
            current.append(ch)
            escape = True
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            current.append(ch)
            quote = ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        if ch == "," and depth == 0:
            chunks.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        chunks.append("".join(current).strip())
    return chunks


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    value = value.replace("<bbox>", "").replace("</bbox>", "")
    value = value.replace("<point>", "").replace("</point>", "")
    return value.strip()


def _coords_from_inputs(
    inputs: dict[str, str],
    key: str,
    *,
    screen_width: int,
    screen_height: int,
    scale_factor: float,
    factors: tuple[float, float],
    origin: tuple[int, int] = (0, 0),
) -> tuple[int, int] | None:
    raw = inputs.get(key)
    if not raw:
        return None
    nums = _numbers(raw)
    if len(nums) < 2:
        return None
    if len(nums) == 2:
        nums = [nums[0], nums[1], nums[0], nums[1]]
    x1, y1, x2, y2 = nums[:4]
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) > 1.0:
        x1, x2 = x1 / factors[0], x2 / factors[0]
        y1, y2 = y1 / factors[1], y2 / factors[1]
    # Clamp to [0, 1] before scaling — a VLM occasionally hallucinates a
    # box at e.g. 1500 (out of 1000), which would otherwise click well
    # outside the screen (or on an adjacent monitor in multi-display setups,
    # which is worse than a no-op because you can't see what happened).
    x1 = min(1.0, max(0.0, x1))
    x2 = min(1.0, max(0.0, x2))
    y1 = min(1.0, max(0.0, y1))
    y2 = min(1.0, max(0.0, y2))
    px = int(round(((x1 + x2) / 2.0) * screen_width * scale_factor))
    py = int(round(((y1 + y2) / 2.0) * screen_height * scale_factor))
    # Translate from screenshot-local pixels into the target coordinate space
    # (virtual desktop for all_screens captures, primary monitor for single).
    return px + int(origin[0]), py + int(origin[1])


def _numbers(raw: str) -> list[float]:
    cleaned = raw.replace("[", " ").replace("]", " ")
    cleaned = cleaned.replace("(", " ").replace(")", " ")
    cleaned = cleaned.replace(",", " ")
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", cleaned)]


def _parsed_as_dict(parsed: ParsedAction) -> dict[str, Any]:
    data = asdict(parsed)
    if parsed.start_coords is not None:
        data["start_coords"] = list(parsed.start_coords)
    if parsed.end_coords is not None:
        data["end_coords"] = list(parsed.end_coords)
    return data


def _execute_desktop_parsed(parsed: ParsedAction) -> dict[str, Any]:
    if sys.platform == "win32":
        return _execute_windows(parsed)
    return _execute_pyautogui(parsed)


def _predict_action_with_vision(image_path: str, *, prompt: str, backend: str) -> str:
    from tools import vision_tools

    return vision_tools.describe_image(image_path, prompt=prompt, backend=backend)


def _execute_windows(parsed: ParsedAction) -> dict[str, Any]:
    import ctypes

    user32 = ctypes.windll.user32
    action = parsed.action_type.lower()
    inputs = parsed.action_inputs

    def move(pos: tuple[int, int] | None) -> None:
        if pos is None:
            raise ValueError(f"{action} requires coordinates")
        user32.SetCursorPos(int(pos[0]), int(pos[1]))
        time.sleep(0.05)

    def mouse_event(flag: int, data: int = 0) -> None:
        user32.mouse_event(flag, 0, 0, data, 0)

    if action in TERMINAL_ACTIONS:
        return {"status": "success", "action": action}
    if action in {"wait"}:
        time.sleep(float(inputs.get("seconds") or 5.0))
        return {"status": "success", "action": action}
    if action in {"click", "left_click", "left_single"}:
        move(parsed.start_coords)
        mouse_event(0x0002)
        mouse_event(0x0004)
    elif action in {"left_double", "double_click"}:
        move(parsed.start_coords)
        for _ in range(2):
            mouse_event(0x0002)
            mouse_event(0x0004)
            time.sleep(0.05)
    elif action in {"right_click", "right_single"}:
        move(parsed.start_coords)
        mouse_event(0x0008)
        mouse_event(0x0010)
    elif action in {"drag", "left_click_drag", "select"}:
        move(parsed.start_coords)
        mouse_event(0x0002)
        time.sleep(0.05)
        move(parsed.end_coords)
        mouse_event(0x0004)
    elif action == "scroll":
        direction = str(inputs.get("direction") or "down").lower()
        if parsed.start_coords is not None:
            move(parsed.start_coords)
        clicks = int(float(inputs.get("clicks") or 5))
        delta = 120 * clicks
        if direction == "down":
            delta = -delta
        if direction in {"left", "right"}:
            mouse_event(0x01000, -delta if direction == "left" else delta)
        else:
            mouse_event(0x0800, delta)
    elif action == "hotkey":
        _windows_hotkey(str(inputs.get("key") or inputs.get("hotkey") or ""))
    elif action in {"press", "release"}:
        keys = _windows_keys(str(inputs.get("key") or inputs.get("hotkey") or ""))
        for key in keys:
            ctypes.windll.user32.keybd_event(key, 0, 0 if action == "press" else 0x0002, 0)
    elif action == "type":
        _windows_type_text(str(inputs.get("content") or ""))
    else:
        return {"status": "error", "error": "unsupported_action", "action": action}
    return {"status": "success", "action": action, "parsed": _parsed_as_dict(parsed)}


def _windows_hotkey(key_str: str) -> None:
    import ctypes

    keys = _windows_keys(key_str)
    for key in keys:
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.02)
    for key in reversed(keys):
        ctypes.windll.user32.keybd_event(key, 0, 0x0002, 0)
        time.sleep(0.02)


def _windows_type_text(content: str) -> None:
    text, submit = _strip_submit_newline(content)
    if text:
        _clipboard_paste_windows(text)
    if submit:
        _windows_hotkey("enter")


def _clipboard_paste_windows(text: str) -> None:
    # tkinter keeps this dependency in the stdlib while supporting Unicode.
    # We snapshot the user's clipboard, paste our payload, then restore.
    # Tk only exposes the text clipboard so non-text contents (images,
    # files) cannot be backed up — we surface that as a recorded warning
    # rather than silently wiping the user's clipboard.
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    previous: str | None = None
    had_non_text_clipboard = False
    try:
        try:
            previous = root.clipboard_get()
        except Exception:
            previous = None
            had_non_text_clipboard = True  # may have been image/files/empty
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        _windows_hotkey("ctrl+v")
        time.sleep(0.05)
    finally:
        if previous is not None:
            with contextlib.suppress(Exception):
                root.clipboard_clear()
                root.clipboard_append(previous)
                root.update()
        elif had_non_text_clipboard:
            # We couldn't restore the original (image/file etc.). Leave a
            # breadcrumb so a confused user can track this down later.
            with contextlib.suppress(Exception):
                from launcher import activity_log
                activity_log.record({
                    "phase": "gui_step",
                    "action": "clipboard_overwrite",
                    "status": "warning",
                    "detail": "previous clipboard contents were non-text and could not be restored",
                })
        with contextlib.suppress(Exception):
            root.destroy()


def _strip_submit_newline(content: str) -> tuple[str, bool]:
    if content.endswith("\\n"):
        return content[:-2], True
    if content.endswith("\n"):
        return content[:-1], True
    return content, False


def _windows_keys(key_str: str) -> list[int]:
    mapping = {
        "ctrl": 0x11,
        "control": 0x11,
        "shift": 0x10,
        "alt": 0x12,
        "enter": 0x0D,
        "return": 0x0D,
        "tab": 0x09,
        "esc": 0x1B,
        "escape": 0x1B,
        "space": 0x20,
        "backspace": 0x08,
        "delete": 0x2E,
        "del": 0x2E,
        "home": 0x24,
        "end": 0x23,
        "pageup": 0x21,
        "page_up": 0x21,
        "pagedown": 0x22,
        "page_down": 0x22,
        "left": 0x25,
        "arrowleft": 0x25,
        "up": 0x26,
        "arrowup": 0x26,
        "right": 0x27,
        "arrowright": 0x27,
        "down": 0x28,
        "arrowdown": 0x28,
        "win": 0x5B,
        "meta": 0x5B,
        "cmd": 0x5B,
        "command": 0x5B,
    }
    parts = [p for p in re.split(r"[\s+]+", key_str.lower().strip()) if p]
    out: list[int] = []
    for part in parts:
        if part in mapping:
            out.append(mapping[part])
        elif len(part) == 1:
            out.append(ord(part.upper()))
        elif re.fullmatch(r"f\d{1,2}", part):
            n = int(part[1:])
            if 1 <= n <= 24:
                out.append(0x70 + n - 1)
    if not out:
        raise ValueError(f"unknown hotkey: {key_str!r}")
    return out


def _execute_pyautogui(parsed: ParsedAction) -> dict[str, Any]:
    try:
        import pyautogui
    except Exception as exc:
        return {
            "status": "error",
            "error": "missing_dependency",
            "detail": f"pyautogui is required for non-Windows desktop control: {exc}",
            "install_hint": "pip install pyautogui",
        }
    action = parsed.action_type.lower()
    inputs = parsed.action_inputs
    if action in TERMINAL_ACTIONS:
        return {"status": "success", "action": action}
    if action == "wait":
        time.sleep(float(inputs.get("seconds") or 5.0))
    elif action in {"click", "left_click", "left_single"}:
        pyautogui.click(*_require_coords(parsed.start_coords))
    elif action in {"left_double", "double_click"}:
        pyautogui.doubleClick(*_require_coords(parsed.start_coords))
    elif action in {"right_click", "right_single"}:
        pyautogui.rightClick(*_require_coords(parsed.start_coords))
    elif action in {"drag", "left_click_drag", "select"}:
        pyautogui.moveTo(*_require_coords(parsed.start_coords))
        pyautogui.dragTo(*_require_coords(parsed.end_coords), duration=0.2, button="left")
    elif action == "scroll":
        clicks = int(float(inputs.get("clicks") or 5))
        direction = str(inputs.get("direction") or "down").lower()
        pyautogui.scroll(clicks if direction == "up" else -clicks)
    elif action == "hotkey":
        pyautogui.hotkey(*[p for p in re.split(r"[\s+]+", str(inputs.get("key") or inputs.get("hotkey") or "")) if p])
    elif action == "type":
        text, submit = _strip_submit_newline(str(inputs.get("content") or ""))
        pyautogui.write(text)
        if submit:
            pyautogui.press("enter")
    else:
        return {"status": "error", "error": "unsupported_action", "action": action}
    return {"status": "success", "action": action, "parsed": _parsed_as_dict(parsed)}


def _require_coords(pos: tuple[int, int] | None) -> tuple[int, int]:
    if pos is None:
        raise ValueError("action requires coordinates")
    return pos


def _record_gui_step(event: dict[str, Any]) -> None:
    try:
        from launcher import activity_log

        activity_log.record({"phase": "gui_step", **event})
    except Exception:
        pass
