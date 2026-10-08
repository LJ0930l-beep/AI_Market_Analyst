"""Prepare a causal PA evidence addition, without editing frozen production code."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.replay.ai_template_runner import frozen_source_fingerprint

TARGET = "core/trading/price_action_structure.py"
FIELD = "prior_swings"
ANCHOR = '        "confirmed_swings": [public_swing(item) for item in (*high_swings, *low_swings)],\n'
ADDITION = '''        # These prices use the same confirmed, available pivots as above.
        # The latest confirmed row already carries the newer price and timing.
        # Preserve the prior confirmed price per side; absent sides stay absent.
        "prior_swings": {
            side: _rounded(items[-2]["price"])
            for side, items in (("HIGH", high_swings), ("LOW", low_swings))
            if len(items) >= 2
        },
'''


def proposed_source():
    original = (ROOT / TARGET).read_text(encoding="utf-8")
    if original.count(ANCHOR) != 1 or FIELD in original:
        raise ValueError("PA_SWING_CONTEXT_SOURCE_ANCHOR_CHANGED")
    return original, original.replace(ANCHOR, ANCHOR + ADDITION, 1)


def proposed_namespace():
    """Test a separate namespace, not an import replacement or file mutation."""
    _, proposed = proposed_source()
    namespace = {"__name__": "prepared_price_action_structure", "__file__": str(ROOT / TARGET)}
    exec(compile(proposed, str(ROOT / TARGET) + ":PREPARED_NOT_APPLIED", "exec"), namespace)
    return namespace


def prepare(directory):
    directory = Path(directory).resolve()
    original, proposed = proposed_source()
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True),
        proposed.splitlines(keepends=True), fromfile="a/" + TARGET, tofile="b/" + TARGET))
    receipt = {"status": "PREPARED_NOT_APPLIED", "target": TARGET,
        "source_file_sha256": hashlib.sha256((ROOT / TARGET).read_bytes()).hexdigest(),
        "source_text_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "proposed_sha256": hashlib.sha256(proposed.encode()).hexdigest(),
        "frozen_source_fingerprint": frozen_source_fingerprint(),
        "field": FIELD, "meaning": "PREVIOUS_CONFIRMED_PRICE_PER_SIDE_LATEST_PRICE_IN_CONFIRMED_SWINGS",
        "maximum_additional_prices_per_side": 1, "future_data_added": False,
        "entry_veto_added": False, "prices_or_trend_labels_generated": False,
        "model_calls": 0, "live_orders": 0, "source_applied": False,
        "activation_requirement": "NO_LIVE_RESEARCH_JOB_AND_OLD_EVIDENCE_PRESERVED_NEW_FREEZE_REQUIRED"}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "next-freeze-swing-context.patch").write_text(patch, encoding="utf-8")
    (directory / "next-freeze-swing-context.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory), indent=2))
