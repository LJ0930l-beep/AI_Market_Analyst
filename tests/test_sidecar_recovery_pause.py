"""Real API startup with saved resume flags; no network or exchange execution."""
import pytest
from fastapi.testclient import TestClient
from core.storage import SQLiteStore
from core.monitoring_runtime import MonitoringRuntime
from apps.api.main import create_app


@pytest.mark.parametrize('via_env', [True, False])
def test_recovery_suppresses_saved_resume_without_rewriting_preferences(tmp_path, monkeypatch, via_env):
    store = SQLiteStore(tmp_path / 'isolated.sqlite3')
    store.initialize()
    store.upsert_app_setting('monitoring.resume', True)
    store.upsert_app_setting('ai.autonomous_resume', True)
    starts = []
    def forbidden_start(self, **kwargs):
        starts.append(kwargs)
        raise AssertionError('Recovery must not start any trading/guardian worker')
    monkeypatch.setattr(MonitoringRuntime, 'start', forbidden_start)
    if via_env:
        monkeypatch.setenv('AIMA_DISABLE_AUTO_RESUME', '1')
    else:
        monkeypatch.delenv('AIMA_DISABLE_AUTO_RESUME', raising=False)
    app = create_app(store=store, llm_provider=None, consult_service=None,
                     market_hydration_enabled=False, daily_brief_schedule_enabled=False,
                     automatic_resume_enabled=None if via_env else False)
    with TestClient(app):
        assert app.state.automatic_resume_suppressed is True
        assert app.state.monitoring_runtime.ai_coordinator._enabled is False
        assert not starts
    assert store.get_app_setting('monitoring.resume')['value'] is True
    assert store.get_app_setting('ai.autonomous_resume')['value'] is True
