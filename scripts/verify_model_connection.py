"""Read-only Gemini connection check; no trading or credential output."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ai.ollama import OllamaProvider
from core.model_routing import DEFAULT_MODEL

def main():
    health = OllamaProvider().health(model_name=DEFAULT_MODEL)
    print(json.dumps({key: health.get(key) for key in (
        "model_id", "available", "model_available", "actual_model_id",
        "model_identity_source", "digest_status", "error_code")}, ensure_ascii=False))
    return 0 if health.get("model_available") is True else 2

if __name__ == "__main__":
    raise SystemExit(main())
