import runpy
import unittest


_smoke = runpy.run_path("scripts/v12-live-smoke.py")
_is_valid_smart_analysis = _smoke["_is_valid_smart_analysis"]
_is_verified_bonsai_health = _smoke["_is_verified_bonsai_health"]


class V12LiveSmokeModelIdentityTests(unittest.TestCase):
    def test_accepts_valid_bonsai_analysis(self):
        self.assertTrue(_is_valid_smart_analysis({
            "model_id": "Bonsai-2-27B-PTQ1_0",
            "validator_status": "VALID",
        }))

    def test_rejects_legacy_qwen_and_invalid_analysis(self):
        self.assertFalse(_is_valid_smart_analysis({"model_id": "qwen3.5:9b", "validator_status": "VALID"}))
        self.assertFalse(_is_valid_smart_analysis({"model_id": "Bonsai-2-27B-PTQ1_0", "validator_status": "INVALID"}))
        self.assertFalse(_is_valid_smart_analysis(None))

    def test_model_health_requires_actual_manifest_identity(self):
        self.assertTrue(_is_verified_bonsai_health({
            "available": True,
            "model_available": True,
            "model_id": "Bonsai-2-27B-PTQ1_0",
            "actual_model_id": "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "model_identity_source": "verified_manifest",
        }))
        self.assertFalse(_is_verified_bonsai_health({
            "available": True,
            "model_available": True,
            "model_id": "Bonsai-2-27B-PTQ1_0",
            "actual_model_id": None,
            "model_identity_source": None,
        }))


if __name__ == "__main__":
    unittest.main()
