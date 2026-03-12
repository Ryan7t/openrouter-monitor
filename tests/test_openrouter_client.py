from __future__ import annotations

import unittest

from openrouter_monitor.openrouter_client import (
    OPENROUTER_CREDITS_URL,
    OPENROUTER_CURRENT_KEY_URL,
    OpenRouterClient,
    OpenRouterClientError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.get_calls: list[tuple[str, dict[str, str], int]] = []

    def get(self, url: str, headers: dict[str, str], timeout: int) -> object:
        self.get_calls.append((url, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class OpenRouterClientTests(unittest.TestCase):
    def test_get_key_metrics_parses_success_response(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "data": {
                            "label": "prod",
                            "is_free_tier": False,
                            "is_management_key": False,
                            "is_provisioning_key": False,
                            "usage": 12.5,
                            "limit": 20,
                            "limit_remaining": None,
                            "limit_reset": None,
                            "expires_at": None,
                            "usage_daily": 1.2,
                            "usage_weekly": 3.4,
                            "usage_monthly": 12.5,
                            "include_byok_in_limit": False,
                            "byok_usage": 0,
                            "byok_usage_daily": 0,
                            "byok_usage_weekly": 0,
                            "byok_usage_monthly": 0,
                            "rate_limit": {"requests": 200, "interval": "10s", "note": "legacy"},
                        }
                    },
                )
            ]
        )
        client = OpenRouterClient(session=session)

        metrics = client.get_key_metrics("key-123")

        self.assertEqual(metrics.label, "prod")
        self.assertIsNone(metrics.limit_remaining)
        self.assertEqual(metrics.rate_limit_requests, 200)
        self.assertEqual(session.get_calls[0][0], OPENROUTER_CURRENT_KEY_URL)
        self.assertEqual(session.get_calls[0][1]["Authorization"], "Bearer key-123")

    def test_get_key_metrics_retries_on_transient_errors(self) -> None:
        sleeps: list[float] = []
        session = FakeSession(
            [
                FakeResponse(503, {"error": {"message": "busy"}}),
                FakeResponse(
                    200,
                    {
                        "data": {
                            "label": "prod",
                            "is_free_tier": False,
                            "is_management_key": False,
                            "is_provisioning_key": False,
                            "usage": 1,
                            "limit": 20,
                            "limit_remaining": 8,
                            "limit_reset": None,
                            "expires_at": None,
                            "usage_daily": 0.2,
                            "usage_weekly": 0.4,
                            "usage_monthly": 1,
                            "include_byok_in_limit": False,
                            "byok_usage": 0,
                            "byok_usage_daily": 0,
                            "byok_usage_weekly": 0,
                            "byok_usage_monthly": 0,
                            "rate_limit": {"requests": 200, "interval": "10s"},
                        }
                    },
                ),
            ]
        )
        client = OpenRouterClient(session=session, sleeper=sleeps.append)

        metrics = client.get_key_metrics("key-123")

        self.assertEqual(metrics.limit_remaining, 8.0)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(len(session.get_calls), 2)

    def test_get_credits_parses_success_response(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "data": {
                            "total_credits": 100,
                            "total_usage": 76.5,
                        }
                    },
                )
            ]
        )
        client = OpenRouterClient(session=session)

        credits = client.get_credits("credits-key")

        self.assertEqual(credits.total_credits, 100.0)
        self.assertEqual(credits.total_usage, 76.5)
        self.assertEqual(session.get_calls[0][0], OPENROUTER_CREDITS_URL)

    def test_get_key_metrics_raises_on_unauthorized(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    401,
                    {"error": {"message": "Invalid key"}},
                    text="Invalid key",
                )
            ]
        )
        client = OpenRouterClient(session=session)

        with self.assertRaises(OpenRouterClientError) as ctx:
            client.get_key_metrics("bad-key")

        self.assertEqual(ctx.exception.kind, "unauthorized")
        self.assertIn("Invalid key", ctx.exception.message)

    def test_get_key_metrics_raises_on_invalid_json(self) -> None:
        session = FakeSession([FakeResponse(200, ValueError("bad json"))])
        client = OpenRouterClient(session=session)

        with self.assertRaises(OpenRouterClientError) as ctx:
            client.get_key_metrics("bad-json")

        self.assertEqual(ctx.exception.kind, "invalid_response")
