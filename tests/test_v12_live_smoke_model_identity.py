import runpy
import unittest


_smoke = runpy.run_path("scripts/v12-live-smoke.py")
_is_valid_smart_analysis = _smoke["_is_valid_smart_analysis"]
_is_verified_gemini_health = _smoke["_is_verified_gemini_health"]
_MODEL_ID = "gemini-3.8-flash-high"


class V12LiveSmokeModelIdentityTests(unittest.TestCase):
    def test_accepts_valid_bonsai_analysis(self):
        self.assertTrue(_is_valid_smart_analysis({
            "model_id": _MODEL_ID,
            "validator_status": "VALID",
        }))

    def test_rejects_legacy_qwen_and_invalid_analysis(self):
        self.assertFalse(_is_valid_smart_analysis({"model_id": "qwen3.5:9b", "validator_status": "VALID"}))
        self.assertFalse(_is_valid_smart_analysis({"model_id": _MODEL_ID, "validator_status": "INVALID"}))
        self.assertFalse(_is_valid_smart_analysis(None))

    def test_model_health_requires_actual_manifest_identity(self):
        self.assertTrue(_is_verified_gemini_health({
            "available": True,
            "model_available": True,
            "model_id": _MODEL_ID,
            "actual_model_id": _MODEL_ID,
            "model_identity_source": "completion_probe",
        }))
        self.assertFalse(_is_verified_gemini_health({
            "available": True,
            "model_available": True,
            "model_id": _MODEL_ID,
            "actual_model_id": None,
            "model_identity_source": None,
        }))


if __name__ == "__main__":
    unittest.main()
