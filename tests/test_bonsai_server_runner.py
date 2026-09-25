"""The workstation launcher must not start a second 27B server on port 8080."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _runner():
    path = Path(__file__).resolve().parents[1] / "infra" / "bonsai" / "server_runner.py"
    spec = spec_from_file_location("bonsai_server_runner_test", path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_existing_verified_server_does_not_spawn_duplicate(monkeypatch):
    runner = _runner()
    monkeypatch.setattr(runner, "load_env_config", lambda: {"BONSAI_HOST": "127.0.0.1", "BONSAI_PORT": "8080"})
    monkeypatch.setattr(runner, "probe_server", lambda _cfg: "READY")
    monkeypatch.setattr(runner, "get_running_pid", lambda: None)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate spawn")))
    assert runner.start_server() is True


def test_foreign_model_on_port_fails_closed(monkeypatch):
    runner = _runner()
    monkeypatch.setattr(runner, "load_env_config", lambda: {"BONSAI_HOST": "127.0.0.1", "BONSAI_PORT": "8080"})
    monkeypatch.setattr(runner, "probe_server", lambda _cfg: "MISMATCH")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("foreign port spawn")))
    assert runner.start_server() is False


def test_occupied_unverified_port_never_spawns_second_server(monkeypatch):
    runner = _runner()
    monkeypatch.setattr(runner, "load_env_config", lambda: {"BONSAI_HOST": "127.0.0.1", "BONSAI_PORT": "8080"})
    monkeypatch.setattr(runner, "probe_server", lambda _cfg: "UNVERIFIED")
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("occupied port spawn")))
    assert runner.start_server() is False
