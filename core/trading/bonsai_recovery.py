"""Bounded recovery of the configured local Bonsai service.

The desktop sync writes the exact Python and runner paths into its installation
directory. Missing configuration never causes an arbitrary executable search.
Only a failed Bonsai health probe may schedule a restart, at most once per five
minutes; the runner itself verifies the model identity on the loopback port.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess
import sys
import threading
import time


logger = logging.getLogger("core.trading.bonsai_recovery")
_LOCK = threading.RLock()
_LAST_ATTEMPT = float("-inf")
_ACTIVE = False
_COOLDOWN_SECONDS = 300


def _configured_command() -> list[str] | None:
    manifest = Path(sys.executable).resolve().parent / "bonsai-recovery.json"
    try:
        config = json.loads(manifest.read_text(encoding="utf-8-sig"))
        python = Path(str(config["python"])).resolve(strict=True)
        runner = Path(str(config["runner"])).resolve(strict=True)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if python.name.lower() != "python.exe" or runner.name != "server_runner.py":
        return None
    if runner.parent.name != "bonsai" or runner.parent.parent.name != "infra":
        return None
    return [str(python), str(runner), "start"]


def schedule_bonsai_recovery() -> bool:
    """Start one detached recovery attempt without delaying the AI cycle."""
    global _LAST_ATTEMPT, _ACTIVE
    command = _configured_command()
    if command is None:
        return False
    with _LOCK:
        now = time.monotonic()
        if _ACTIVE or now - _LAST_ATTEMPT < _COOLDOWN_SECONDS:
            return False
        _LAST_ATTEMPT = now
        _ACTIVE = True

    def recover() -> None:
        global _ACTIVE
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=100,
                cwd=str(Path(command[1]).parent.parent.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                logger.info("Bonsai recovery runner verified the local model service")
            else:
                logger.warning("Bonsai recovery failed: exit=%s stderr=%s", completed.returncode, completed.stderr[-500:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Bonsai recovery could not start: %s", exc)
        finally:
            with _LOCK:
                _ACTIVE = False

    threading.Thread(target=recover, name="bonsai-recovery", daemon=True).start()
    return True
