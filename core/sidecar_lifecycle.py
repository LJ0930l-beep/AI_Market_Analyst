"""Sidecar process lifecycle manager with strict PID isolation (AT28).

Guarantees:
- Manages local backend/sidecar processes safely under Windows/POSIX.
- Strictly tracks and terminates ONLY its own child PID.
- NEVER issues global process kills (e.g. taskkill /IM python.exe or killing external Ollama).
- Robust handling of paths with spaces or Unicode / Chinese characters.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class SidecarState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    CRASHED = "CRASHED"
    TERMINATED = "TERMINATED"


class SidecarProcessManager:
    """Manages a single child sidecar process with strict PID isolation."""

    def __init__(self, name: str = "api_sidecar") -> None:
        self.name = name
        self._child_pid: Optional[int] = None
        self._process: Optional[subprocess.Popen] = None
        self._state: SidecarState = SidecarState.STOPPED
        self._exit_code: Optional[int] = None

    @property
    def child_pid(self) -> Optional[int]:
        return self._child_pid

    @property
    def state(self) -> SidecarState:
        self._refresh_state()
        return self._state

    def is_running(self) -> bool:
        return self.state == SidecarState.RUNNING

    def _refresh_state(self) -> None:
        if self._process is None or self._child_pid is None:
            if self._state not in (SidecarState.STOPPED, SidecarState.TERMINATED):
                self._state = SidecarState.STOPPED
            return

        ret = self._process.poll()
        if ret is None:
            self._state = SidecarState.RUNNING
        else:
            self._exit_code = ret
            if self._state != SidecarState.TERMINATED:
                self._state = SidecarState.CRASHED if ret != 0 else SidecarState.TERMINATED

    def spawn(
        self,
        command: List[str],
        cwd: Optional[str | Path] = None,
        env: Optional[dict] = None,
    ) -> int:
        """Spawn a child process, tracking its exact PID."""
        if self.is_running():
            raise RuntimeError(f"Sidecar {self.name} is already running with PID {self._child_pid}")

        work_dir = Path(cwd) if cwd else Path.cwd()
        if not work_dir.exists():
            raise FileNotFoundError(f"Working directory does not exist: {work_dir}")

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        self._state = SidecarState.STARTING
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(work_dir),
                env=merged_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._child_pid = self._process.pid
            self._state = SidecarState.RUNNING
            logger.info("Spawned sidecar %s with PID %s", self.name, self._child_pid)
            return self._child_pid
        except Exception as exc:
            self._state = SidecarState.CRASHED
            logger.error("Failed to spawn sidecar %s: %s", self.name, exc)
            raise

    def terminate_own_pid_only(self, timeout_seconds: float = 3.0) -> bool:
        """Strictly terminate only the registered child PID.

        Never runs broad taskkill commands against python.exe or ollama.exe.
        """
        if self._process is None or self._child_pid is None:
            self._state = SidecarState.TERMINATED
            return True

        target_pid = self._child_pid
        logger.info("Terminating sidecar %s (PID %s) strictly by PID", self.name, target_pid)

        # Poll if already dead
        if self._process.poll() is not None:
            self._state = SidecarState.TERMINATED
            self._child_pid = None
            self._process = None
            return True

        # Attempt graceful termination
        try:
            self._process.terminate()
        except OSError:
            pass

        # Wait for exit
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._state = SidecarState.TERMINATED
                self._child_pid = None
                self._process = None
                return True
            time.sleep(0.1)

        # Force kill ONLY target PID if still active
        try:
            if sys.platform == "win32":
                # Windows taskkill strictly by PID
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(target_pid)],
                    capture_output=True,
                    timeout=5.0,
                    check=False,
                )
            else:
                self._process.kill()
        except Exception as exc:
            logger.warning("Force kill of PID %s encountered: %s", target_pid, exc)

        self._state = SidecarState.TERMINATED
        self._child_pid = None
        self._process = None
        return True
