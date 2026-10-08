from copy import deepcopy
import json

import pytest

from core.storage import SQLiteStore
from core.trading.ai_strategy_book import (
    AIStrategyBook, ACTIVE_TEMPLATES as TEMPLATES, STRATEGY_PROFILE_VERSION, _V8_NOFX_STYLE_BRIEFS,
)
from core.trading.autonomous_strategy import build_strategy_system_prompt, GEMINI_HIGH_DECISION_GUIDE
from core.trading.ai_session_coordinator import _compact_gate_system_prompt


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda item: item["id"])
def test_saved_v8_strategy_updates_real_prompt_without_changing_operator_budget(tmp_path, template):
    store = SQLiteStore(tmp_path / "strategy.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    execution = {**book.active("gate_live")["execution"], "leverage": 37,
                 "max_margin_pct": 17, "max_notional_usdt": 500, "fixed_notional_usdt": 500}
    saved = book.save("gate_live", name=template["name"], template_id=template["id"],
                      expected_revision=0, execution=execution, sections=deepcopy(template["sections"]))
    legacy = deepcopy(saved["sections"])
    legacy["entry_standards"] = _V8_NOFX_STYLE_BRIEFS[template["id"]]
    legacy["custom_prompt"] = "操作员补充：仅使用已核验账户，不伪造新闻。"
    with store._connect() as db:
        db.execute("UPDATE ai_strategy_instructions SET sections_json=? WHERE account_id=?",
                   (json.dumps(legacy, ensure_ascii=False), "gate_live"))
    active = book.active("gate_live")
    assert active["sections"]["entry_standards"] == template["sections"]["entry_standards"]
    assert active["sections"]["custom_prompt"] == legacy["custom_prompt"]
    assert active["profile"]["prompt_version"] == STRATEGY_PROFILE_VERSION
    assert active["execution"]["leverage"] == 37
    assert active["execution"]["max_margin_pct"] == 17
    assert active["execution"]["max_notional_usdt"] == 2000
    assert active["execution"]["scan_interval_minutes"] == template["scan_interval_minutes"]
    assert active["revision"] == saved["revision"]
    prompt = _compact_gate_system_prompt(build_strategy_system_prompt(active, nofx_gate=True), active)
    assert template["sections"]["entry_standards"] in prompt
    assert GEMINI_HIGH_DECISION_GUIDE in prompt
    assert "操作员补充" in prompt
    assert "ownership=VERIFIED_SYSTEM" in prompt
    assert "UPDATE_PROTECTION" in prompt and "CANCEL_ORDER" in prompt


def test_operator_authored_strategy_is_not_replaced_by_model_upgrade(tmp_path):
    store = SQLiteStore(tmp_path / "custom.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    sections = {**deepcopy(TEMPLATES[0]["sections"]), "entry_standards": "自定义：仅评估已核验事件后的回测。"}
    saved = book.save("gate_live", name="自定义关注点", template_id=TEMPLATES[0]["id"],
                      expected_revision=0, sections=sections, execution=book.active("gate_live")["execution"])
    assert book.active("gate_live")["sections"] == saved["sections"] == sections
