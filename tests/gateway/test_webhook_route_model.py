"""Per-route inference overrides for webhook-triggered agent runs."""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def test_route_pins_model_provider_and_reasoning_before_dispatch():
    asyncio.run(_exercise_route_model_override())


async def _exercise_route_model_override():
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "structured-event": {
                        "secret": _INSECURE_NO_AUTH,
                        "prompt": "Event: {kind}",
                        "provider": "openai-codex",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "low",
                    }
                },
            },
        )
    )
    runner = MagicMock()
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._session_key_for_source.side_effect = lambda source: source.chat_id
    adapter.gateway_runner = runner

    captured: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        captured.append(event)

    adapter.handle_message = capture

    with patch(
        "hermes_cli.model_switch.switch_model",
        return_value=SimpleNamespace(
            success=True,
            new_model="gpt-5.6-luna",
            target_provider="openai-codex",
            api_key="resolved-oauth-secret",
            base_url="https://chatgpt.com/backend-api/codex",
            api_mode="codex_responses",
            error_message="",
        ),
    ) as switch:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/webhooks/structured-event",
                data=json.dumps({"kind": "created"}).encode(),
                headers={"Content-Type": "application/json", "X-Request-ID": "route-model-1"},
            )
            assert response.status == 202

        await asyncio.sleep(0.05)
        switch.assert_called_once()
    assert len(captured) == 1
    session_key = captured[0].source.chat_id
    assert runner._session_model_overrides[session_key] == {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "api_key": "resolved-oauth-secret",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }
    assert runner._session_reasoning_overrides[session_key] == {
        "enabled": True,
        "effort": "low",
    }

    await adapter.on_processing_complete(captured[0], None)
    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides


def test_resolution_failure_does_not_consume_delivery_id():
    asyncio.run(_exercise_resolution_failure_retry())


async def _exercise_resolution_failure_retry():
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "broken-pin": {
                        "secret": _INSECURE_NO_AUTH,
                        "prompt": "Event",
                        "provider": "openai-codex",
                        "model": "missing-model",
                    }
                },
            },
        )
    )
    runner = MagicMock()
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._session_key_for_source.side_effect = lambda source: source.chat_id
    adapter.gateway_runner = runner
    adapter.handle_message = MagicMock()

    failed = SimpleNamespace(success=False, error_message="model unavailable")
    with patch("hermes_cli.model_switch.switch_model", return_value=failed) as switch:
        async with TestClient(TestServer(_app(adapter))) as client:
            headers = {"Content-Type": "application/json", "X-Request-ID": "retryable-id"}
            first = await client.post("/webhooks/broken-pin", json={}, headers=headers)
            second = await client.post("/webhooks/broken-pin", json={}, headers=headers)
        assert first.status == 503
        assert second.status == 503
        assert switch.call_count == 2
    adapter.handle_message.assert_not_called()


def test_route_resolution_cache_expires():
    asyncio.run(_exercise_route_resolution_cache_expiry())


async def _exercise_route_resolution_cache_expiry():
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    route = {"provider": "openai-codex", "model": "gpt-5.6-luna"}
    resolved = SimpleNamespace(
        success=True,
        new_model="gpt-5.6-luna",
        target_provider="openai-codex",
        api_key="rotating-token",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        error_message="",
    )
    adapter._route_inference_cache_ttl_seconds = 0.01
    with patch("hermes_cli.model_switch.switch_model", return_value=resolved) as switch:
        first, first_error = await adapter._resolve_route_inference_pin("route", route)
        second, second_error = await adapter._resolve_route_inference_pin("route", route)
        await asyncio.sleep(0.02)
        third, third_error = await adapter._resolve_route_inference_pin("route", route)
    assert first_error is second_error is third_error is None
    assert first == second == third
    assert switch.call_count == 2


def test_route_resolution_runs_inside_target_profile_scope():
    asyncio.run(_exercise_target_profile_scope())


async def _exercise_target_profile_scope():
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    route = {
        "profile": "sami",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
    }
    seen = []
    profile_home = Path("/tmp/hermes-profile-sami")

    @contextmanager
    def fake_scope(home):
        seen.append(("enter", home))
        try:
            yield
        finally:
            seen.append(("exit", home))

    def fake_switch(**_kwargs):
        seen.append(("switch", None))
        return SimpleNamespace(
            success=True,
            new_model="gpt-5.6-luna",
            target_provider="openai-codex",
            api_key="sami-oauth-secret",
            base_url="https://chatgpt.com/backend-api/codex",
            api_mode="codex_responses",
            error_message="",
        )

    with (
        patch.object(adapter, "_profile_home_for_route", return_value=profile_home),
        patch("gateway.run._profile_runtime_scope", fake_scope),
        patch("hermes_cli.model_switch.switch_model", side_effect=fake_switch),
    ):
        resolved, error = await adapter._resolve_route_inference_pin("sami-route", route)

    assert error is None
    assert resolved is not None
    assert resolved["api_key"] == "sami-oauth-secret"
    assert seen == [
        ("enter", profile_home),
        ("switch", None),
        ("exit", profile_home),
    ]


def test_route_cache_signature_includes_profile_home():
    asyncio.run(_exercise_route_cache_profile_rebind())


async def _exercise_route_cache_profile_rebind():
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    route = {
        "profile": "sami",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
    }
    homes = iter((Path("/tmp/sami-a"), Path("/tmp/sami-a"), Path("/tmp/sami-b")))
    resolved = SimpleNamespace(
        success=True,
        new_model="gpt-5.6-luna",
        target_provider="openai-codex",
        api_key="scoped-secret",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        error_message="",
    )

    @contextmanager
    def fake_scope(_home):
        yield

    with (
        patch.object(adapter, "_profile_home_for_route", side_effect=lambda _r: next(homes)),
        patch("gateway.run._profile_runtime_scope", fake_scope),
        patch("hermes_cli.model_switch.switch_model", return_value=resolved) as switch,
    ):
        await adapter._resolve_route_inference_pin("route", route)
        await adapter._resolve_route_inference_pin("route", route)
        await adapter._resolve_route_inference_pin("route", route)

    assert switch.call_count == 2


def test_profile_home_for_route_uses_named_profile_and_process_default():
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    named = Path("/tmp/hermes-named-sami")
    default = Path("/tmp/hermes-process-default")
    with (
        patch("hermes_cli.profiles.profile_exists", return_value=True) as exists,
        patch("hermes_cli.profiles.get_profile_dir", return_value=named) as profile_dir,
        patch("hermes_constants.get_process_hermes_home", return_value=default),
    ):
        assert adapter._profile_home_for_route({"profile": " sami "}) == named
        assert adapter._profile_home_for_route({}) == default
    exists.assert_called_once_with("sami")
    profile_dir.assert_called_once_with("sami")


def test_missing_route_profile_fails_closed_without_switching():
    asyncio.run(_exercise_missing_route_profile())


async def _exercise_missing_route_profile():
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    route = {
        "profile": "deleted-profile",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
    }
    with (
        patch("hermes_cli.profiles.profile_exists", return_value=False),
        patch("hermes_cli.model_switch.switch_model") as switch,
    ):
        resolved, error = await adapter._resolve_route_inference_pin("route", route)
    assert resolved is None
    assert error == "route profile 'deleted-profile' does not exist"
    switch.assert_not_called()


def test_reload_signature_evicts_invalid_profile_without_raising():
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    route = {
        "profile": "deleted-profile",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
    }
    with patch("hermes_cli.profiles.profile_exists", return_value=False):
        signature = adapter._route_cache_signature(route)
    assert signature == (
        "openai-codex",
        "gpt-5.6-luna",
        "<invalid-route-profile>",
    )
