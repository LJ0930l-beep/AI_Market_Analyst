"""Entry points must initialize in fresh interpreters without network calls."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("modules", [
    ("core.model_client",),
    ("core.ai.transport_diagnostics", "core.model_client"),
    ("core.ai.ollama", "core.model_client"),
    ("core.model_client", "core.ai", "core.ai.transport_diagnostics"),
])
def test_model_client_import_order(modules):
    script = "import importlib\n" + "\n".join(f"importlib.import_module({module!r})" for module in modules)
    script += "\nfrom core.model_client import ModelClient, model_client\nassert isinstance(model_client, ModelClient)\n"
    run = subprocess.run([sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
