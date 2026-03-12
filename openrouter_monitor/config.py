from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .models import (
    AlertsConfig,
    AppConfig,
    DefaultsConfig,
    FailureAlertConfig,
    FeishuConfig,
    ServiceConfig,
    StateConfig,
    UserThresholds,
)


DEFAULT_THRESHOLDS = {
    "warning": 10.0,
    "danger": 5.0,
    "critical": 1.0,
}


class ConfigError(ValueError):
    """YAML 配置文件无效时抛出。"""


def load_config(path: str | Path) -> AppConfig:
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Unable to read config file: {file_path}") from exc

    try:
        raw_config = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config file: {file_path}") from exc

    if not isinstance(raw_config, dict):
        raise ConfigError("Config root must be a mapping.")

    service = _parse_service(raw_config.get("service") or {})
    defaults = _parse_defaults(raw_config.get("defaults") or {})
    alerts = _parse_alerts(raw_config.get("alerts") or {})
    feishu = _parse_feishu(raw_config.get("feishu"))
    state = _parse_state(raw_config.get("state") or {})

    return AppConfig(
        service=service,
        defaults=defaults,
        alerts=alerts,
        feishu=feishu,
        state=state,
    )


def _parse_service(raw_service: object) -> ServiceConfig:
    if not isinstance(raw_service, dict):
        raise ConfigError("service must be a mapping.")

    poll_interval_minutes = _require_int(
        raw_service.get("poll_interval_minutes", 60),
        "service.poll_interval_minutes",
        minimum=1,
    )
    timezone_name = _require_str(
        raw_service.get("timezone", "Asia/Shanghai"),
        "service.timezone",
    )
    _validate_timezone(timezone_name)
    return ServiceConfig(
        poll_interval_minutes=poll_interval_minutes,
        timezone=timezone_name,
    )


def _parse_defaults(raw_defaults: object) -> DefaultsConfig:
    if not isinstance(raw_defaults, dict):
        raise ConfigError("defaults must be a mapping.")

    push_time = _parse_time(raw_defaults.get("push_time", "10:45"), "defaults.push_time")
    thresholds = _parse_threshold_values(raw_defaults.get("thresholds") or {}, "defaults.thresholds")
    return DefaultsConfig(push_time=push_time, thresholds=thresholds)


def _parse_alerts(raw_alerts: object) -> AlertsConfig:
    if not isinstance(raw_alerts, dict):
        raise ConfigError("alerts must be a mapping.")

    balance_dedupe_hours = _require_int(
        raw_alerts.get("balance_dedupe_hours", 24),
        "alerts.balance_dedupe_hours",
        minimum=1,
    )
    raw_failure = raw_alerts.get("failure") or {}
    if not isinstance(raw_failure, dict):
        raise ConfigError("alerts.failure must be a mapping.")
    failure = FailureAlertConfig(
        dedupe_hours=_require_int(
            raw_failure.get("dedupe_hours", 24),
            "alerts.failure.dedupe_hours",
            minimum=1,
        ),
        critical_after_failures=_require_int(
            raw_failure.get("critical_after_failures", 3),
            "alerts.failure.critical_after_failures",
            minimum=1,
        ),
    )
    return AlertsConfig(balance_dedupe_hours=balance_dedupe_hours, failure=failure)


def _parse_feishu(raw_feishu: object) -> FeishuConfig:
    if not isinstance(raw_feishu, dict):
        raise ConfigError("feishu must be a mapping.")

    app_id = _require_str(raw_feishu.get("app_id"), "feishu.app_id")
    app_secret = _require_str(raw_feishu.get("app_secret"), "feishu.app_secret")
    return FeishuConfig(app_id=app_id, app_secret=app_secret)


def _parse_state(raw_state: object) -> StateConfig:
    if not isinstance(raw_state, dict):
        raise ConfigError("state must be a mapping.")

    users_path = _require_str(raw_state.get("users_path", "data/users.json"), "state.users_path")
    runtime_path = _require_str(raw_state.get("runtime_path", "data/runtime_state.json"), "state.runtime_path")
    return StateConfig(users_path=users_path, runtime_path=runtime_path)


def _parse_threshold_values(raw_thresholds: object, path: str) -> UserThresholds:
    if not isinstance(raw_thresholds, dict):
        raise ConfigError(f"{path} must be a mapping.")

    warning = _require_float(raw_thresholds.get("warning", DEFAULT_THRESHOLDS["warning"]), f"{path}.warning", 0.0)
    danger = _require_float(raw_thresholds.get("danger", DEFAULT_THRESHOLDS["danger"]), f"{path}.danger", 0.0)
    critical = _require_float(
        raw_thresholds.get("critical", DEFAULT_THRESHOLDS["critical"]),
        f"{path}.critical",
        0.0,
    )
    _validate_threshold_order(warning, danger, critical, path)
    return UserThresholds(warning=warning, danger=danger, critical=critical)


def _validate_threshold_order(warning: float, danger: float, critical: float, path: str) -> None:
    if not (warning >= danger >= critical):
        raise ConfigError(f"{path} must satisfy warning >= danger >= critical.")


def _parse_time(value: object, path: str) -> time:
    text = _require_str(value, path)
    parts = text.split(":")
    if len(parts) != 2:
        raise ConfigError(f"{path} must use HH:MM format.")
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as exc:
        raise ConfigError(f"{path} must use HH:MM format.") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError(f"{path} must use a valid 24-hour time.")
    return time(hour=hour, minute=minute)


def _validate_timezone(timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone: {timezone_name}") from exc


def _require_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string.")
    return value.strip()


def _require_int(value: object, path: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be an integer.")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{path} must be >= {minimum}.")
    return value


def _require_float(value: object, path: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number.")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ConfigError(f"{path} must be >= {minimum}.")
    return result
