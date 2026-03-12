from __future__ import annotations

from collections.abc import Callable
from typing import Any
import time

import requests

from .models import AccountCredits, KeyMetrics

OPENROUTER_CURRENT_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"


class OpenRouterClientError(RuntimeError):
    def __init__(self, kind: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


class OpenRouterClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: int = 10,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper or time.sleep

    def get_key_metrics(self, api_key: str) -> KeyMetrics:
        headers = {"Authorization": f"Bearer {api_key}"}
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(
                    OPENROUTER_CURRENT_KEY_URL,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < self.retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise OpenRouterClientError("network_error", f"OpenRouter request failed: {exc}") from exc

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise OpenRouterClientError(
                        "invalid_response",
                        "OpenRouter returned non-JSON data.",
                    ) from exc
                return self._parse_key_metrics(payload)

            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                self._sleep_before_retry(attempt)
                continue

            raise self._build_error(response)

        raise OpenRouterClientError("unknown_error", "OpenRouter request failed without a result.")

    def get_credits(self, api_key: str) -> AccountCredits:
        headers = {"Authorization": f"Bearer {api_key}"}
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(
                    OPENROUTER_CREDITS_URL,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < self.retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise OpenRouterClientError("network_error", f"OpenRouter request failed: {exc}") from exc

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise OpenRouterClientError(
                        "invalid_response",
                        "OpenRouter returned non-JSON data.",
                    ) from exc
                return self._parse_credits(payload)

            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                self._sleep_before_retry(attempt)
                continue

            raise self._build_error(response)

        raise OpenRouterClientError("unknown_error", "OpenRouter request failed without a result.")

    def _sleep_before_retry(self, attempt: int) -> None:
        self.sleeper(self.backoff_seconds * (2 ** (attempt - 1)))

    def _parse_key_metrics(self, payload: Any) -> KeyMetrics:
        data = _require_data_mapping(payload)
        rate_limit = data.get("rate_limit")
        rate_limit_requests: int | None = None
        rate_limit_interval: str | None = None
        rate_limit_note: str | None = None
        if isinstance(rate_limit, dict):
            requests_value = rate_limit.get("requests")
            if isinstance(requests_value, int):
                rate_limit_requests = requests_value
            interval_value = rate_limit.get("interval")
            if isinstance(interval_value, str):
                rate_limit_interval = interval_value
            note_value = rate_limit.get("note")
            if isinstance(note_value, str):
                rate_limit_note = note_value

        return KeyMetrics(
            label=_as_str(data.get("label"), default=""),
            is_free_tier=_as_bool(data.get("is_free_tier")),
            is_management_key=_as_optional_bool(data.get("is_management_key")),
            is_provisioning_key=_as_optional_bool(data.get("is_provisioning_key")),
            usage=_as_float(data.get("usage")),
            limit=_as_optional_float(data.get("limit")),
            limit_remaining=_as_optional_float(data.get("limit_remaining")),
            limit_reset=_as_optional_str(data.get("limit_reset")),
            expires_at=_as_optional_str(data.get("expires_at")),
            usage_daily=_as_float(data.get("usage_daily")),
            usage_weekly=_as_float(data.get("usage_weekly")),
            usage_monthly=_as_float(data.get("usage_monthly")),
            include_byok_in_limit=_as_bool(data.get("include_byok_in_limit")),
            byok_usage=_as_float(data.get("byok_usage")),
            byok_usage_daily=_as_float(data.get("byok_usage_daily")),
            byok_usage_weekly=_as_float(data.get("byok_usage_weekly")),
            byok_usage_monthly=_as_float(data.get("byok_usage_monthly")),
            rate_limit_requests=rate_limit_requests,
            rate_limit_interval=rate_limit_interval,
            rate_limit_note=rate_limit_note,
        )

    def _parse_credits(self, payload: Any) -> AccountCredits:
        data = _require_data_mapping(payload)
        return AccountCredits(
            total_credits=_as_float(data.get("total_credits")),
            total_usage=_as_float(data.get("total_usage")),
        )

    def _build_error(self, response: requests.Response) -> OpenRouterClientError:
        status_code = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = _extract_error_message(payload) or response.text.strip() or "No details returned."
        if status_code == 401:
            return OpenRouterClientError("unauthorized", f"OpenRouter rejected the key: {detail}", status_code)
        if status_code == 403:
            return OpenRouterClientError("forbidden", f"OpenRouter denied the request: {detail}", status_code)
        return OpenRouterClientError(f"http_{status_code}", f"OpenRouter returned HTTP {status_code}: {detail}", status_code)


def _require_data_mapping(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise OpenRouterClientError("invalid_response", "OpenRouter returned an invalid response.")
    return payload["data"]


def _extract_error_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def _as_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise OpenRouterClientError("invalid_response", "OpenRouter returned an invalid boolean field.")
    return value


def _as_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _as_bool(value)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise OpenRouterClientError("invalid_response", "OpenRouter returned an invalid string field.")
    return value


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return _as_str(value)


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenRouterClientError("invalid_response", "OpenRouter returned an invalid numeric field.")
    return float(value)


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _as_float(value)
