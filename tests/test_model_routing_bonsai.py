from __future__ import annotations

import copy

import pytest

from core.model_routing import (
    DEFAULT_MODEL,
    is_trusted_model_provider,
    is_verified_model_receipt,
)

INPUT_HASH = "a" * 64


def _receipt() -> dict[str, str]:
    return {
        "model_id": DEFAULT_MODEL,
        "actual_model_id": DEFAULT_MODEL,
        "model_version": DEFAULT_MODEL,
        "model_identity_source": "completion_response",
        "verified_manifest_model_id": DEFAULT_MODEL,
        "prompt_version": "agent_v2",
        "input_hash": INPUT_HASH,
        "parse_status": "valid",
    }


def test_bonsai_trade_receipt_is_bound_to_exact_prompt_and_input() -> None:
    assert is_verified_model_receipt(
        _receipt(),
        expected_prompt_version="agent_v2",
        expected_input_hash=INPUT_HASH,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"model_identity_source": "request_bound_to_verified_manifest"},
        {"actual_model_id": "qwen3.5:9b"},
        {"model_version": "qwen3.5:9b"},
        {"verified_manifest_model_id": "qwen3.5:9b"},
        {"input_hash": "another-request"},
        {"prompt_version": "different-prompt"},
        {"parse_status": "invalid"},
        {"verified_manifest_model_id": None},
        {"actual_model_id": None},
    ],
    ids=["change0", "change1", "change2", "change3", "change4", "change5", "change6", "change7", "missing_actual"],
)
def test_bonsai_trade_receipt_rejects_forged_identity_or_unbound_response(change) -> None:
    receipt = _receipt()
    receipt.update(copy.deepcopy(change))
    assert not is_verified_model_receipt(
        receipt,
        expected_prompt_version="agent_v2",
        expected_input_hash=INPUT_HASH,
    )


def test_bonsai_trade_receipt_rejects_partial_binding_expectations() -> None:
    assert not is_verified_model_receipt(
        _receipt(), expected_prompt_version="agent_v2"
    )


def test_only_exact_provider_on_pinned_loopback_route_is_trusted() -> None:
    from core.ai.ollama import OllamaProvider, model_client

    trusted = OllamaProvider(base_url=model_client.base_url, model_name=DEFAULT_MODEL)
    qwen_route = OllamaProvider(base_url=model_client.base_url, model_name="qwen3.5:9b")
    remote_route = OllamaProvider(base_url="https://example.invalid/v1", model_name=DEFAULT_MODEL)

    class WrappedProvider(OllamaProvider):
        pass

    assert is_trusted_model_provider(trusted)
    assert not is_trusted_model_provider(qwen_route)
    assert not is_trusted_model_provider(remote_route)
    assert not is_trusted_model_provider(WrappedProvider(base_url=model_client.base_url, model_name=DEFAULT_MODEL))
