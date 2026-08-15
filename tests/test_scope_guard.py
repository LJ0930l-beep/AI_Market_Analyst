import unittest
from pathlib import Path


class ScopeGuardTests(unittest.TestCase):
    def test_phase1_runtime_has_no_forbidden_integration_tokens(self):
        root = Path(__file__).resolve().parents[1]
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for base in (root / "core", root / "apps", root / "scripts")
            for path in base.rglob("*.py")
        )
        for forbidden in ("TELEGRAM_BOT_TOKEN", "TINKOFF_INVEST_TOKEN", "CELERY_BROKER_URL", "BROKER_URL"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

