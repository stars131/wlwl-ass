"""Background runtime for GUI operator visual runs.

The tool-level ``gui_operator`` call stays synchronous for the agent loop.
The GUI needs a non-blocking control surface, so this module wraps
``tools.gui_operator.run_visual_task`` in a tiny thread registry with
pause/resume/stop state.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GuiRun:
    run_id: str
    instruction: str
    max_loop: int
    loop_wait: float
    dry_run: bool
    backend: str
    all_screens: bool
    include_base64: bool
    status: str = "queued"
    run_dir: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    _pause_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._pause_event.set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "instruction": self.instruction,
            "max_loop": self.max_loop,
            "loop_wait": self.loop_wait,
            "dry_run": self.dry_run,
            "backend": self.backend,
            "all_screens": self.all_screens,
            "include_base64": self.include_base64,
            "status": self.status,
            "run_dir": self.run_dir,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": self.steps,
            "result": self.result,
        }


class GuiOperatorRegistry:
    # Bounded in-memory registry — terminated runs are evicted oldest-first
    # once we cross MAX_RUNS, so a long-lived launcher process does not leak
    # screenshots / step payloads forever.
    MAX_RUNS = 50
    TERMINAL_STATUSES = frozenset({"finished", "dry_run", "error", "stopped"})

    def __init__(self) -> None:
        self._runs: dict[str, GuiRun] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        instruction: str,
        max_loop: int = 5,
        loop_wait: float = 1.0,
        dry_run: bool = True,
        backend: str = "auto",
        all_screens: bool = False,
        include_base64: bool = False,
    ) -> GuiRun:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("instruction is required")
        run = GuiRun(
            run_id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8],
            instruction=instruction,
            max_loop=max(1, int(max_loop or 1)),
            loop_wait=max(0.0, float(loop_wait or 0.0)),
            dry_run=bool(dry_run),
            backend=backend or "auto",
            all_screens=bool(all_screens),
            include_base64=bool(include_base64),
        )
        with self._lock:
            self._runs[run.run_id] = run
            self._evict_locked()
        thread = threading.Thread(target=self._worker, args=(run,), name=f"gui-run-{run.run_id}", daemon=True)
        run._thread = thread
        thread.start()
        return run

    def _evict_locked(self) -> None:
        """Caller must hold ``self._lock``. Evicts oldest terminated runs."""
        over = len(self._runs) - self.MAX_RUNS
        if over <= 0:
            return
        terminated = [
            run for run in self._runs.values() if run.status in self.TERMINAL_STATUSES
        ]
        terminated.sort(key=lambda r: r.updated_at)
        for run in terminated:
            if over <= 0:
                break
            self._runs.pop(run.run_id, None)
            over -= 1

    def list(self) -> list[GuiRun]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    def get(self, run_id: str) -> GuiRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def pause(self, run_id: str) -> GuiRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        if run.status in {"running", "queued"}:
            run.status = "paused"
            run.updated_at = time.time()
            run._pause_event.clear()
        return run

    def resume(self, run_id: str) -> GuiRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        if run.status == "paused":
            run.status = "running"
            run.updated_at = time.time()
            run._pause_event.set()
        return run

    def stop(self, run_id: str) -> GuiRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        if run.status not in {"finished", "dry_run", "error", "stopped"}:
            run.status = "stopping"
            run.updated_at = time.time()
            run._stop_event.set()
            run._pause_event.set()
        return run

    def _worker(self, run: GuiRun) -> None:
        from tools import gui_operator

        run.status = "running"
        run.updated_at = time.time()

        def should_stop() -> bool:
            return run._stop_event.is_set()

        def wait_if_paused() -> bool:
            while not run._pause_event.is_set():
                if should_stop():
                    return False
                run.updated_at = time.time()
                time.sleep(0.2)
            return True

        def on_step(step: dict[str, Any]) -> None:
            run.steps.append(step)
            observe = step.get("observe") if isinstance(step, dict) else None
            if isinstance(observe, dict) and observe.get("path"):
                run.run_dir = str(Path(str(observe.get("path"))).parent)
            run.updated_at = time.time()

        try:
            result = gui_operator.run_visual_task(
                run.instruction,
                run_id=run.run_id,
                max_loop=run.max_loop,
                loop_wait=run.loop_wait,
                dry_run=run.dry_run,
                include_base64=run.include_base64,
                all_screens=run.all_screens,
                backend=run.backend,
                should_stop=should_stop,
                wait_if_paused=wait_if_paused,
                on_step=on_step,
            )
            run.result = result
            run.run_dir = str(result.get("run_dir") or run.run_dir)
            run.status = str(result.get("status") or "finished")
        except Exception as exc:
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            if run.status == "stopping":
                run.status = "stopped"
            run.updated_at = time.time()


_REGISTRY = GuiOperatorRegistry()


def registry() -> GuiOperatorRegistry:
    return _REGISTRY
