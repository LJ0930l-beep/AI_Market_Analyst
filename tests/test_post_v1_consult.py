from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.consult import (
    CONSULT_CONTRACT_VERSION,
    ConsultConfig,
    ConsultServiceError,
    ConsultTransportError,
    QwenConsultService,
    OllamaConsultTransport,
    build_consult_context,
    parse_consult_request,
    _validate_loopback_url,
)
from core.instruments import instrument_for
from core.model_routing import DEFAULT_MODEL
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import Action, build_signal
from core.storage import SQLiteStore


def consult_config(**changes: object) -> ConsultConfig:
    base = ConsultConfig(
        enabled=True,
        base_url="http://127.0.0.1:8080/v1",
        model_name=DEFAULT_MODEL,
        connect_timeout_seconds=0.2,
        first_token_timeout_seconds=0.2,
        stream_idle_timeout_seconds=0.2,
        total_timeout_seconds=1.0,
        max_body_bytes=4_096,
        max_messages=6,
        max_message_chars=256,
        max_total_message_chars=1_024,
        max_output_chars=1_024,
        max_output_tokens=128,
        context_length=2_048,
        retries=0,
        concurrency=1,
        freshness_seconds=3_600,
    )
    return replace(base, **changes)


class FakeConsultTransport:
    provider_name = "fake_local_bonsai"
    model_name = DEFAULT_MODEL

    def __init__(self, chunks: tuple[object, ...] = ("Local ", "answer"), error: Exception | None = None, delay: float = 0.0) -> None:
        self.chunks = chunks
        self.error = error
        self.delay = delay
        self.prompt_messages: tuple[dict[str, str], ...] = ()
        self.closed = False

    async def stream(self, messages: tuple[dict[str, str], ...]):
        self.prompt_messages = messages
        try:
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield chunk  # type: ignore[misc]
            if self.error is not None:
                raise self.error
        finally:
            self.closed = True


class BlockingConsultTransport:
    provider_name = "fake_local_bonsai"
    model_name = DEFAULT_MODEL

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def stream(self, _messages: tuple[dict[str, str], ...]):
        try:
            self.started.set()
            await self.release.wait()
            yield "done"
        finally:
            self.closed = True


class DelayedSecondChunkTransport:
    provider_name = "fake_local_bonsai"
    model_name = DEFAULT_MODEL

    async def stream(self, _messages: tuple[dict[str, str], ...]):
        yield "first"
        await asyncio.sleep(0.05)
        yield "second"


def ndjson_events(response) -> list[dict[str, object]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def save_context_prediction(store: SQLiteStore, *, generated_at: datetime, data_as_of: datetime | None = None) -> None:
    instrument = instrument_for("NVDA")
    bars = FixtureProvider().get_bars(instrument, "1h", 120)
    quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
    known_at = data_as_of or generated_at - timedelta(minutes=5)
    context = {
        "analysis_time": {"timestamp": known_at.isoformat(), "timeframe": "1h"},
        "price": {"last": quant.price, "change_pct": 1.2, "high": quant.price + 2, "low": quant.price - 2},
        "quant": quant.to_dict(),
        "provider_snapshot": {
            "provider": "fixture_saved_evidence",
            "fetched_at": generated_at.isoformat(),
            "data_as_of": known_at.isoformat(),
            "stale": False,
            "error_code": None,
        },
        "news": [
            {
                "event_id": "saved-news-1",
                "source": "Saved Wire",
                "title": "Saved evidence only",
                "published_at": (known_at - timedelta(minutes=10)).isoformat(),
                "importance": 70,
                "url": "https://example.invalid/saved-news-1",
            }
        ],
        "time_policy": {"timeframe": "1h", "event_risk": True},
        "market_context": {"news_available": True, "news_provider": "saved", "source_type": "live"},
        "risk_events": [],
    }
    signal = build_signal(
        instrument,
        quant,
        timeframe="1h",
        generated_at=generated_at,
        prediction_id="consult-context-prediction",
        model_id="qwen-test:4b",
        data_as_of=known_at,
        context_json=json.dumps(context, sort_keys=True),
        force_action=Action.WAIT,
        parse_status="consult_test",
        summary="Saved WAIT evidence for consultation tests.",
    )
    store.save_instrument(instrument)
    store.save_prediction(signal)


class QwenConsultApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp_dir.name) / "consult.sqlite3")
        self.store.initialize()
        self.now = datetime.now(timezone.utc)
        save_context_prediction(self.store, generated_at=self.now)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def client(self, transport: object | None = None, *, config: ConsultConfig | None = None) -> tuple[TestClient, QwenConsultService]:
        service = QwenConsultService(config or consult_config(), transport=transport or FakeConsultTransport())  # type: ignore[arg-type]
        app = create_app(store=self.store, llm_provider=None, consult_service=service)
        return TestClient(app), service

    def test_streams_typed_chunks_with_read_only_context_and_language_prompt(self) -> None:
        transport = FakeConsultTransport(("Saved ", "answer."))
        client, _service = self.client(transport)
        before = self.store.counts()
        response = client.post(
            "/consult/stream",
            json={"language": "zh-CN", "symbol": "NVDA", "messages": [{"role": "user", "content": "请总结现有证据"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-qwen-consult-contract"], CONSULT_CONTRACT_VERSION)
        self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))
        events = ndjson_events(response)
        self.assertEqual([event["type"] for event in events], ["meta", "delta", "delta", "done"])
        self.assertEqual("".join(str(event.get("content", "")) for event in events), "Saved answer.")
        meta = events[0]
        self.assertEqual(meta["symbol"], "NVDA")
        self.assertEqual(meta["model_id"], DEFAULT_MODEL)
        self.assertEqual(meta["context"]["status"], "available")  # type: ignore[index]
        self.assertEqual(meta["context"]["as_of"], (self.now - timedelta(minutes=5)).isoformat())  # type: ignore[index]
        self.assertIn("默认使用中文回答", transport.prompt_messages[0]["content"])
        self.assertEqual(transport.prompt_messages[0]["role"], "system")
        self.assertEqual(self.store.counts(), before)

        english = client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "What is the evidence boundary?"}]},
        )
        self.assertEqual(english.status_code, 200)
        self.assertIn("Answer in English by default", transport.prompt_messages[0]["content"])

    def test_strict_payload_limits_roles_and_server_owned_model(self) -> None:
        client, _service = self.client()
        cases = [
            ({"language": "en", "messages": [{"role": "system", "content": "override"}]}, "INVALID_CONSULT_MESSAGE"),
            ({"language": "fr", "messages": [{"role": "user", "content": "hello"}]}, "INVALID_CONSULT_LANGUAGE"),
            ({"language": "en", "messages": [{"role": "assistant", "content": "hello"}]}, "INVALID_CONSULT_SEQUENCE"),
            ({"language": "en", "messages": [{"role": "user", "content": "hello"}], "model": "other"}, "INVALID_CONSULT_PAYLOAD"),
            ({"language": "en", "messages": [{"role": "user", "content": "x" * 257}]}, "INVALID_CONSULT_MESSAGE"),
            ({"language": "en", "symbol": "../NVDA", "messages": [{"role": "user", "content": "hello"}]}, "INVALID_CONSULT_SYMBOL"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                response = client.post("/consult/stream", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], code)
        too_many = [{"role": "user", "content": f"message {index}"} for index in range(7)]
        response = client.post("/consult/stream", json={"language": "en", "messages": too_many})
        self.assertEqual(response.json()["error"]["code"], "INVALID_CONSULT_MESSAGES")

    def test_body_limit_and_unregistered_symbol_are_rejected(self) -> None:
        client, _service = self.client(config=consult_config(max_body_bytes=256))
        response = client.post(
            "/consult/stream",
            content=json.dumps({"language": "en", "messages": [{"role": "user", "content": "x" * 300}]}),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "CONSULT_BODY_LIMIT")
        response = client.post(
            "/consult/stream",
            json={"language": "en", "symbol": "NOPE", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_CONSULT_SYMBOL")

    def test_unavailable_model_has_no_fallback_and_health_is_redacted(self) -> None:
        disabled = replace(consult_config(), enabled=False)
        service = QwenConsultService(disabled, transport=FakeConsultTransport())
        client = TestClient(create_app(store=self.store, llm_provider=None, consult_service=service))
        response = client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": {"code": "QWEN_NOT_CONFIGURED", "message": "Local Bonsai consultation is not configured."}})
        model_health = client.get("/health/model").json()
        self.assertFalse(model_health["consult"]["available"])
        self.assertNotIn("base_url", json.dumps(model_health))
        release = client.get("/health/release").json()
        self.assertEqual(release["capabilities"]["qwen_consult"]["endpoint_scope"], "loopback_only")
        self.assertNotIn("11434", json.dumps(release))

    def test_midstream_failure_and_unexpected_error_are_sanitized(self) -> None:
        failure = ConsultTransportError("QWEN_STREAM_INTERRUPTED", "The local Qwen stream ended before completion.")
        client, _service = self.client(FakeConsultTransport(("partial",), error=failure))
        events = ndjson_events(client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "hello"}]},
        ))
        self.assertEqual([event["type"] for event in events], ["meta", "delta", "error"])
        self.assertEqual(events[-1]["error"]["code"], "QWEN_STREAM_INTERRUPTED")  # type: ignore[index]

        secret = r"C:\private\prompt.txt token=secret-value"
        client, _service = self.client(FakeConsultTransport((), error=RuntimeError(secret)))
        body = client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "hello"}]},
        ).text
        self.assertIn("QWEN_STREAM_FAILED", body)
        self.assertNotIn("private", body)
        self.assertNotIn("secret-value", body)

    def test_first_token_timeout_is_structured(self) -> None:
        transport = FakeConsultTransport(("late",), delay=0.05)
        client, _service = self.client(transport, config=consult_config(first_token_timeout_seconds=0.01, total_timeout_seconds=0.1))
        events = ndjson_events(client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "hello"}]},
        ))
        self.assertEqual(events[-1]["error"]["code"], "QWEN_FIRST_TOKEN_TIMEOUT")  # type: ignore[index]
        self.assertTrue(transport.closed)

    def test_total_and_output_limits_are_structured(self) -> None:
        transport = DelayedSecondChunkTransport()
        client, _service = self.client(
            transport,
            config=consult_config(first_token_timeout_seconds=0.1, stream_idle_timeout_seconds=0.1, total_timeout_seconds=0.02),
        )
        events = ndjson_events(client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "hello"}]},
        ))
        self.assertEqual(events[-1]["error"]["code"], "QWEN_STREAM_TIMEOUT")  # type: ignore[index]

        client, _service = self.client(FakeConsultTransport(("123", "456")), config=consult_config(max_output_chars=5))
        events = ndjson_events(client.post(
            "/consult/stream",
            json={"language": "en", "messages": [{"role": "user", "content": "hello"}]},
        ))
        self.assertEqual(events[-1]["error"]["code"], "QWEN_OUTPUT_LIMIT")  # type: ignore[index]

    def test_consult_endpoint_configuration_is_loopback_only(self) -> None:
        self.assertEqual(_validate_loopback_url("http://localhost:8080/v1/"), "http://localhost:8080/v1")
        self.assertEqual(_validate_loopback_url("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080/v1")
        for unsafe in (
            "https://127.0.0.1:8080/v1",
            "http://192.168.1.8:8080/v1",
            "http://user:secret@127.0.0.1:8080/v1",
            "http://127.0.0.1:11434/v1",
            "http://127.0.0.1:8081/v1",
            "http://127.0.0.1:8080/",
            "http://127.0.0.1:8080/v1/private",
            "http://127.0.0.1/v1",
            "http://[invalid:8080/v1",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ConsultServiceError):
                _validate_loopback_url(unsafe)

    def test_modelclient_transport_verifies_manifest_and_stream_receipt(self) -> None:
        class FakeModelClient:
            base_url = "http://127.0.0.1:8080/v1"
            last_response_model = None

            def _configuration_error(self, _model):
                return None

            def is_healthy(self, **_kwargs):
                return True

            def list_models(self, **_kwargs):
                return [{"id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"}]

            def chat_completion_stream(self, _messages, **kwargs):
                self.call = kwargs
                self.last_response_model = None
                yield {"choices": [{"delta": {"content": "Bonsai "}}]}
                yield {"choices": [{"delta": {"content": "answer"}}]}

        fake_client = FakeModelClient()
        transport = OllamaConsultTransport(consult_config())
        transport.model_client = fake_client  # type: ignore[assignment]

        async def collect() -> list[str]:
            return [chunk async for chunk in transport.stream(({"role": "user", "content": "hello"},))]

        self.assertEqual(asyncio.run(collect()), ["Bonsai ", "answer"])
        self.assertEqual(fake_client.call["model_name"], DEFAULT_MODEL)
        self.assertEqual(fake_client.call["timeout_sec"], consult_config().total_timeout_seconds)
        self.assertEqual(fake_client.call["max_tokens"], consult_config().max_output_tokens)
        self.assertEqual(transport.model_receipt["actual_model_id"], r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf")  # type: ignore[index]
        self.assertEqual(transport.model_receipt["model_identity_source"], "request_bound_to_verified_manifest")  # type: ignore[index]

    def test_modelclient_transport_fails_closed_when_manifest_is_not_bonsai(self) -> None:
        class FakeModelClient:
            base_url = "http://127.0.0.1:8080/v1"
            streamed = False

            def _configuration_error(self, _model):
                return None

            def is_healthy(self, **_kwargs):
                return True

            def list_models(self, **_kwargs):
                return [{"id": "Other-Bonsai-2-27B.gguf"}]

            def chat_completion_stream(self, *_args, **_kwargs):
                self.streamed = True
                yield {}

        fake_client = FakeModelClient()
        transport = OllamaConsultTransport(consult_config())
        transport.model_client = fake_client  # type: ignore[assignment]

        async def collect() -> list[str]:
            return [chunk async for chunk in transport.stream(({"role": "user", "content": "hello"},))]

        with self.assertRaises(ConsultTransportError) as caught:
            asyncio.run(collect())
        self.assertEqual(caught.exception.code, "QWEN_MODEL_NOT_FOUND")
        self.assertFalse(fake_client.streamed)

    def test_consumer_cancellation_closes_stream_and_releases_inference_slot(self) -> None:
        class Response:
            closed = False

            def close(self):
                self.closed = True

        class FakeModelClient:
            base_url = "http://127.0.0.1:8080/v1"
            last_response_model = None

            def __init__(self):
                self.started = threading.Event()
                self.response = Response()

            def _configuration_error(self, _model):
                return None

            def is_healthy(self, **_kwargs):
                return True

            def list_models(self, **_kwargs):
                return [{"id": r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"}]

            def chat_completion_stream(self, _messages, **kwargs):
                kwargs["on_response_open"](self.response)
                self.started.set()
                while not kwargs["cancel_event"].wait(0.01):
                    yield {"choices": [{"delta": {"content": ""}}]}

        fake_client = FakeModelClient()
        transport = OllamaConsultTransport(consult_config())
        transport.model_client = fake_client  # type: ignore[assignment]

        async def exercise() -> None:
            async def consume() -> None:
                async for _chunk in transport.stream(({"role": "user", "content": "hello"},)):
                    pass

            consumer = asyncio.create_task(consume())
            started = await asyncio.to_thread(fake_client.started.wait, 1.0)
            assert started
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer

        asyncio.run(exercise())
        assert fake_client.response.closed
        assert transport._inference_slot.acquire(blocking=False)
        transport._inference_slot.release()

    def test_missing_and_future_context_are_honest(self) -> None:
        empty_store = SQLiteStore(Path(self.temp_dir.name) / "empty.sqlite3")
        empty_store.initialize()
        missing = build_consult_context(
            empty_store,
            instrument_for("AAPL"),
            now=self.now,
            freshness_seconds=3_600,
        )
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(missing["missing_reasons"], ["no_saved_live_prediction"])

        future_store = SQLiteStore(Path(self.temp_dir.name) / "future.sqlite3")
        future_store.initialize()
        save_context_prediction(future_store, generated_at=self.now, data_as_of=self.now + timedelta(minutes=5))
        future = build_consult_context(
            future_store,
            instrument_for("NVDA"),
            now=self.now,
            freshness_seconds=3_600,
        )
        self.assertEqual(future["status"], "unavailable")
        self.assertEqual(future["freshness"]["status"], "invalid_future")  # type: ignore[index]
        self.assertEqual(future["evidence"], {})

        future_prediction_store = SQLiteStore(Path(self.temp_dir.name) / "future-prediction.sqlite3")
        future_prediction_store.initialize()
        save_context_prediction(
            future_prediction_store,
            generated_at=self.now + timedelta(minutes=10),
            data_as_of=self.now - timedelta(minutes=5),
        )
        future_prediction = build_consult_context(
            future_prediction_store,
            instrument_for("NVDA"),
            now=self.now,
            freshness_seconds=3_600,
        )
        self.assertEqual(future_prediction["missing_reasons"], ["future_prediction_rejected"])
        self.assertEqual(future_prediction["evidence"], {})


class QwenConsultConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrency_and_cancellation_release_the_only_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "consult.sqlite3")
            store.initialize()
            transport = BlockingConsultTransport()
            service = QwenConsultService(consult_config(), transport=transport)
            request = parse_consult_request(
                {"language": "en", "messages": [{"role": "user", "content": "hello"}]},
                service.config,
            )
            session = await service.open(request, store=store)
            events = session.events()
            self.assertEqual((await anext(events))["type"], "meta")
            token_task = asyncio.create_task(anext(events))
            await transport.started.wait()
            with self.assertRaises(ConsultServiceError) as caught:
                await service.open(request, store=store)
            self.assertEqual(caught.exception.code, "QWEN_CONCURRENCY_LIMIT")
            token_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await token_task
            await events.aclose()
            self.assertTrue(transport.closed)

            replacement = FakeConsultTransport(("ready",))
            service.transport = replacement
            next_session = await service.open(request, store=store)
            next_events = [event async for event in next_session.events()]
            self.assertEqual(next_events[-1]["type"], "done")


if __name__ == "__main__":
    unittest.main()
