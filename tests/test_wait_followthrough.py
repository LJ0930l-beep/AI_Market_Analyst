"""Synthetic trigger diagnostics; these are not real trading evidence."""
import json
import pytest
from scripts.audit_gemini_wait_followthrough import analyze_waits


def row(minute, *, action="WAIT", status="COMPLETED", trigger=100, symbol="BTCUSDT"):
    return {"template_id": "fixture", "as_of": f"2025-01-01T00:{minute:02d}:00+00:00",
        "submission_at": f"2025-01-01T00:{minute+1:02d}:00+00:00", "status": status,
        "decision_json": json.dumps({"action": action, "instrument_id": symbol,
            "next_trigger_price": trigger, "strategy_analysis": {"missing_conditions": ["volume confirmation"]}}),
        "result_json": json.dumps({"status": "SUBMITTED" if action.startswith("OPEN") else "WAITING"})}


def bar(minute, *, high=101, low=99, available=None):
    return {"symbol": "BTCUSDT", "timeframe": "1m", "low": low, "high": high,
        "bar_end": f"2025-01-01T00:{minute:02d}:00+00:00",
        "available_at": f"2025-01-01T00:{available or minute:02d}:00+00:00"}


def test_price_touch_does_not_prove_full_setup_or_demand_entry():
    report = analyze_waits([row(0), row(5)], [bar(3)])
    assert report[0]["status"] == "PRICE_TOUCHED_SETUP_NOT_PROVEN"
    assert report[0]["next_action"] == "WAIT" and not report[0]["full_setup_confirmed"]


def test_future_and_model_thinking_bars_cannot_be_counted_as_touches():
    report = analyze_waits([row(0), row(5)], [bar(1), bar(6), bar(3, available=7)])
    assert report[0]["status"] == "NO_PRICE_TOUCH_BEFORE_NEXT_SCAN"


def test_unfinished_next_scan_is_pending_not_a_missed_trade():
    report = analyze_waits([row(0), row(5, status="MODEL_STARTED")], [bar(3)])
    assert report[0]["status"] == "AWAITING_NEXT_SCAN"


def test_order_submission_and_symbol_change_are_preserved():
    report = analyze_waits([row(0), row(5, action="OPEN_SHORT", symbol="ETHUSDT")], [bar(3)])
    assert report[0]["next_execution_status"] == "SUBMITTED"
    assert report[0]["next_same_symbol"] is False


def test_absent_trigger_is_unverifiable_not_silently_inferred():
    report = analyze_waits([row(0, trigger=None), row(5)], [bar(3)])
    assert report[0]["status"] == "UNVERIFIABLE_TRIGGER"
    assert "NO_VERIFIABLE_TRIGGER_PRICE" in report[0]["issues"]


def test_unknown_price_trigger_keeps_next_actual_action_without_fabricating_price():
    report=analyze_waits([row(0,trigger=None),row(5,action='OPEN_LONG')],[bar(3)])
    assert report[0]['status']=='UNVERIFIABLE_TRIGGER'
    assert report[0]['next_action']=='OPEN_LONG' and report[0]['next_execution_status']=='SUBMITTED'
    assert report[0]['first_price_touch_at'] is None and not report[0]['full_setup_confirmed']


@pytest.mark.parametrize('defect',['none','wrong_plan','outside_path'])
def test_directory_requires_same_research_plan_and_owned_window(tmp_path,defect):
    import sqlite3
    from core.replay.ai_history import digest,manifest_hash
    from scripts.audit_gemini_wait_followthrough import audit_directory
    window_id='../other' if defect=='outside_path' else 'pilot-1'
    plan={'pilot_windows':[{'id':window_id,'partition':'optimization'}]}
    plan_hash=digest(plan)
    (tmp_path/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':plan_hash}),encoding='utf-8')
    if defect=='outside_path':
        with pytest.raises(ValueError,match='PATH_OUTSIDE_EXPERIMENT'): audit_directory(tmp_path)
        return
    target=tmp_path/'pilot-1';target.mkdir()
    history={'bars':[],'research_plan_sha256':'other' if defect=='wrong_plan' else plan_hash}
    history['manifest_sha256']=manifest_hash(history)
    (target/'history.json').write_text(json.dumps(history),encoding='utf-8')
    with sqlite3.connect(target/'results.sqlite3') as db:
        db.execute('CREATE TABLE ai_template_replay_decisions(status)')
    if defect=='wrong_plan':
        with pytest.raises(ValueError,match='HISTORY_PLAN_MISMATCH'): audit_directory(tmp_path)
    else:
        report=audit_directory(tmp_path)
        assert report['windows'][0]['findings']==[]
