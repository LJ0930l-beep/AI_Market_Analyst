"""Local structured-model boundary for Phase 2.

The package contains only an Ollama HTTP adapter, deterministic contracts, and
validation. It never places orders or stores credentials.
"""

from .contracts import LLMCallMetadata, LLMError, ModelSignalResponse, SignalPolicy
from .mock import MockLLMProvider
from .ollama import OllamaProvider
from .prompts import PROMPT_VERSION, build_prompt_messages, build_repair_messages, build_system_prompt
from .validator import SignalValidationError, analyze_with_repair, to_signal_proposal, validate_model_response

__all__ = [
    "LLMCallMetadata",
    "LLMError",
    "ModelSignalResponse",
    "MockLLMProvider",
    "OllamaProvider",
    "PROMPT_VERSION",
    "SignalPolicy",
    "SignalValidationError",
    "analyze_with_repair",
    "build_prompt_messages",
    "build_repair_messages",
    "build_system_prompt",
    "to_signal_proposal",
    "validate_model_response",
]
