import json
import subprocess
import threading

import core.trading.bonsai_recovery as recovery


def test_recovery_uses_pinned_local_runner_and_throttles_attempts(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    sidecar = install / "ai-market-analyst-backend.exe"
    sidecar.touch()
    python = tmp_path / "python.exe"
    python.touch()
    runner = tmp_path / "infra" / "bonsai" / "server_runner.py"
    runner.parent.mkdir(parents=True)
    runner.touch()
    (install / "bonsai-recovery.json").write_text(json.dumps({"python": str(python), "runner": str(runner)}), encoding="utf-8")
    monkeypatch.setattr(recovery.sys, "executable", str(sidecar))
    monkeypatch.setattr(recovery, "_LAST_ATTEMPT", float("-inf"))
    monkeypatch.setattr(recovery, "_ACTIVE", False)
    called = threading.Event()
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        called.set()
        return subprocess.CompletedProcess(command, 0, "healthy", "")

    monkeypatch.setattr(recovery.subprocess, "run", run)
    assert recovery.schedule_bonsai_recovery() is True
    assert called.wait(2)
    assert len(commands) == 1
    assert commands[0][0] == [str(python), str(runner), "start"]
    assert commands[0][1]["cwd"] == str(tmp_path)
    assert recovery.schedule_bonsai_recovery() is False


def test_recovery_requires_manifest_and_exact_runner_path(tmp_path, monkeypatch):
    sidecar = tmp_path / "ai-market-analyst-backend.exe"
    sidecar.touch()
    monkeypatch.setattr(recovery.sys, "executable", str(sidecar))
    assert recovery.schedule_bonsai_recovery() is False
    (tmp_path / "bonsai-recovery.json").write_text(json.dumps({"python": str(sidecar), "runner": str(sidecar)}), encoding="utf-8")
    assert recovery.schedule_bonsai_recovery() is False
