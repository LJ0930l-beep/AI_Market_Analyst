"""Read deployed functions for isolated regression tests; never apply a patch."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('core/replay/ai_template_runner.py','core/replay/gemini_research.py',
         'core/trading/ai_session_coordinator.py','core/model_client.py',
         'core/ai/ollama.py','core/trading/model_schemas.py')

def active_sources():
    sources = {name:(ROOT/name).read_text(encoding='utf-8') for name in FILES}
    return dict(sources), sources
