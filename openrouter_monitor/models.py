from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(slots=True, frozen=True)
class ServiceConfig:
    poll_interval_minutes: int
    timezone: str
    interval_quiet_hours: QuietHoursConfig | None


@dataclass(slots=True, frozen=True)
class QuietHoursConfig:
    start: time
    end: time


@dataclass(slots=True, frozen=True)
class UserThresholds:
    warning: float
    danger: float
    critical: float


@dataclass(slots=True, frozen=True)
class DefaultsConfig:
    push_time: time
    push_interval_minutes: int | None
    thresholds: UserThresholds


@dataclass(slots=True, frozen=True)
class FailureAlertConfig:
    dedupe_hours: int
    critical_after_failures: int


@dataclass(slots=True, frozen=True)
class AlertsConfig:
    balance_dedupe_hours: int
    failure: FailureAlertConfig


@dataclass(slots=True, frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str


@dataclass(slots=True, frozen=True)
class StateConfig:
    users_path: str
    runtime_path: str
    snapshots_path: str


@dataclass(slots=True, frozen=True)
class AppConfig:
    service: ServiceConfig
    defaults: DefaultsConfig
    alerts: AlertsConfig
    feishu: FeishuConfig
    state: StateConfig


@dataclass(slots=True, frozen=True)
class UserIdentity:
    open_id: str
    user_id: str | None = None
    union_id: str | None = None
    display_name: str | None = None


@dataclass(slots=True, frozen=True)
class KeyMetrics:
    label: str
    is_free_tier: bool
    is_management_key: bool | None
    is_provisioning_key: bool | None
    usage: float
    limit: float | None
    limit_remaining: float | None
    limit_reset: str | None
    expires_at: str | None
    usage_daily: float
    usage_weekly: float
    usage_monthly: float
    include_byok_in_limit: bool
    byok_usage: float
    byok_usage_daily: float
    byok_usage_weekly: float
    byok_usage_monthly: float
    rate_limit_requests: int | None = None
    rate_limit_interval: str | None = None
    rate_limit_note: str | None = None


@dataclass(slots=True, frozen=True)
class AccountCredits:
    total_credits: float
    total_usage: float


@dataclass(slots=True, frozen=True)
class UserConfigUpdate:
    push_time: time
    push_interval_minutes: int | None
    push_interval_quiet_hours: QuietHoursConfig | None
    thresholds: UserThresholds
