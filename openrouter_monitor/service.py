from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .feishu import FeishuAppClient
from .messages import (
    build_bind_success_message,
    build_config_message,
    build_config_updated_message,
    build_delete_success_message,
    build_detail_key_section,
    build_detail_report,
    build_failure_alert_message,
    build_no_keys_message,
    build_threshold_alert_message,
)
from .models import AppConfig, KeyMetrics, QuietHoursConfig, UserIdentity, UserThresholds
from .openrouter_client import OpenRouterClient, OpenRouterClientError
from .state_store import BalanceTrendStore, RuntimeStateStore, UserStore
from .utils import (
    dedupe_expired,
    format_currency,
    format_hhmm,
    hash_api_key,
    iso_or_none,
    mask_api_key,
    parse_datetime,
    parse_iso_date,
    parse_time_value,
)


LOGGER = logging.getLogger(__name__)
SCHEDULER_MISFIRE_GRACE_SECONDS = 60


class UserCommandError(ValueError):
    """用户命令或用户状态不满足要求时抛出。"""


class MonitorService:
    def __init__(
        self,
        config: AppConfig,
        openrouter_client: OpenRouterClient | None = None,
        notifier: FeishuAppClient | None = None,
        user_store: UserStore | None = None,
        runtime_store: RuntimeStateStore | None = None,
        trend_store: BalanceTrendStore | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.zoneinfo = ZoneInfo(config.service.timezone)
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.notifier = notifier or FeishuAppClient(
            app_id=config.feishu.app_id,
            app_secret=config.feishu.app_secret,
        )
        self.user_store = user_store or UserStore(config.state.users_path)
        self.runtime_store = runtime_store or RuntimeStateStore(config.state.runtime_path)
        self.trend_store = trend_store or BalanceTrendStore(config.state.trends_path)
        self.now_factory = now_factory or self._now
        self._user_lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._trend_lock = threading.Lock()
        self._scheduler: BackgroundScheduler | None = None

    def start_scheduler(self) -> None:
        if self._scheduler is not None:
            return
        scheduler = BackgroundScheduler(timezone=self.zoneinfo)
        scheduler.add_job(
            self.safe_threshold_scan,
            trigger="interval",
            minutes=self.config.service.poll_interval_minutes,
            id="threshold-scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=SCHEDULER_MISFIRE_GRACE_SECONDS,
        )
        scheduler.add_job(
            self.safe_daily_detail_dispatch,
            trigger="cron",
            minute="*",
            id="daily-detail-dispatch",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=SCHEDULER_MISFIRE_GRACE_SECONDS,
        )
        scheduler.start()
        self._scheduler = scheduler
        LOGGER.info("Started background scheduler for user-centered OpenRouter monitoring.")

    def stop_scheduler(self) -> None:
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        LOGGER.info("Stopped background scheduler.")

    def safe_threshold_scan(self) -> None:
        try:
            self.threshold_scan()
        except Exception:
            LOGGER.exception("Unexpected error while scanning thresholds.")

    def safe_daily_detail_dispatch(self) -> None:
        try:
            self.daily_detail_dispatch()
        except Exception:
            LOGGER.exception("Unexpected error while dispatching daily detail reports.")

    def bind_key(self, identity: UserIdentity, api_key: str, alias: str | None = None) -> str:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise UserCommandError("Key 不能为空，例如: /绑定 sk-or-v1-xxx 我的Key")
        normalized_alias = self._normalize_alias(alias)
        now = self.now_factory()
        key_id = hash_api_key(normalized_key)

        with self._user_lock:
            state = self.user_store.load()
            users = self._ensure_users_mapping(state)
            user_entry = users.setdefault(identity.open_id, self._new_user_entry(identity))
            self._merge_identity(user_entry, identity)
            self._ensure_user_defaults(user_entry)
            keys = self._get_key_records(user_entry)
            had_keys_before = bool(keys)
            self._assert_alias_available(keys, normalized_alias, key_id)

            existing = next((item for item in keys if item.get("key_id") == key_id), None)
            existed = existing is not None
            if existing is None:
                existing = {
                    "key_id": key_id,
                    "api_key": normalized_key,
                    "alias": normalized_alias,
                    "created_at": iso_or_none(now),
                    "updated_at": iso_or_none(now),
                }
                keys.append(existing)
            else:
                existing["api_key"] = normalized_key
                if normalized_alias is not None:
                    existing["alias"] = normalized_alias
                existing["updated_at"] = iso_or_none(now)

            settings = self._ensure_settings_mapping(user_entry)
            settings["push_enabled"] = True
            self.user_store.save(state)

        push_time = self._read_push_time(self._ensure_settings_mapping(user_entry))
        push_interval_minutes = self._read_push_interval_minutes(self._ensure_settings_mapping(user_entry))
        push_interval_quiet_hours = self._read_push_interval_quiet_hours(
            self._ensure_settings_mapping(user_entry)
        )
        thresholds = self._read_thresholds(self._ensure_settings_mapping(user_entry))
        if not had_keys_before and push_interval_minutes is not None:
            self._set_next_interval_push_at(
                identity.open_id,
                self._calculate_next_interval_push_at(now, push_interval_minutes),
            )
        return build_bind_success_message(
            alias=existing.get("alias"),
            masked_key=mask_api_key(normalized_key),
            push_time=push_time,
            push_interval_minutes=push_interval_minutes,
            interval_quiet_hours=push_interval_quiet_hours,
            thresholds=thresholds,
            existed=existed,
        )

    def delete_key(self, open_id: str, selector: str) -> str:
        normalized_selector = selector.strip()
        if not normalized_selector:
            raise UserCommandError("请指定要删除的 Key，例如: /删除 备注名 或 /删除 完整Key")

        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._get_user_entry(state, open_id)
            if user_entry is None or not self._get_key_records(user_entry):
                raise UserCommandError(build_no_keys_message())

            keys = self._get_key_records(user_entry)
            target = self._match_key_for_delete(keys, normalized_selector)
            keys.remove(target)
            settings = self._ensure_settings_mapping(user_entry)
            if not keys:
                settings["push_enabled"] = False
            self.user_store.save(state)

        with self._runtime_lock:
            runtime_state = self.runtime_store.load()
            runtime_user = self._get_runtime_user(runtime_state, open_id)
            if runtime_user is not None:
                runtime_keys = runtime_user.setdefault("keys", {})
                runtime_keys.pop(target["key_id"], None)
                if not runtime_keys:
                    runtime_state.get("users", {}).pop(open_id, None)
                self.runtime_store.save(runtime_state)

        return build_delete_success_message(
            alias=target.get("alias"),
            masked_key=mask_api_key(str(target["api_key"])),
            push_enabled=bool(keys),
        )

    def get_user_config_message(self, open_id: str) -> str:
        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._get_user_entry(state, open_id)
            if user_entry is None:
                return build_config_message(
                    push_time=self.config.defaults.push_time,
                    push_interval_minutes=self.config.defaults.push_interval_minutes,
                    interval_quiet_hours=self.config.service.interval_quiet_hours,
                    thresholds=self.config.defaults.thresholds,
                    push_enabled=False,
                    key_count=0,
                )
            settings = self._ensure_settings_mapping(user_entry)
            return build_config_message(
                push_time=self._read_push_time(settings),
                push_interval_minutes=self._read_push_interval_minutes(settings),
                interval_quiet_hours=self._read_push_interval_quiet_hours(settings),
                thresholds=self._read_thresholds(settings),
                push_enabled=bool(settings.get("push_enabled", False)),
                key_count=len(self._get_key_records(user_entry)),
            )

    def update_push_time(self, identity: UserIdentity, push_time: time) -> str:
        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._ensure_user_entry(state, identity)
            settings = self._ensure_settings_mapping(user_entry)
            settings["push_time"] = format_hhmm(push_time)
            self.user_store.save(state)
        return build_config_updated_message("每日推送时间", format_hhmm(push_time))

    def update_push_interval(self, identity: UserIdentity, minutes: int | None) -> str:
        now = self.now_factory()
        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._ensure_user_entry(state, identity)
            settings = self._ensure_settings_mapping(user_entry)
            settings["push_interval_minutes"] = minutes
            can_schedule = bool(settings.get("push_enabled", False)) and bool(self._get_key_records(user_entry))
            self.user_store.save(state)

        if minutes is None:
            self._clear_next_interval_push_at(identity.open_id)
            return build_config_updated_message("间隔推送", "已关闭")

        if can_schedule:
            self._set_next_interval_push_at(
                identity.open_id,
                self._calculate_next_interval_push_at(now, minutes),
            )
        return build_config_updated_message("间隔推送", f"每 {minutes} 分钟一次")

    def update_push_interval_quiet_hours(
        self,
        identity: UserIdentity,
        quiet_hours: QuietHoursConfig | None,
    ) -> str:
        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._ensure_user_entry(state, identity)
            settings = self._ensure_settings_mapping(user_entry)
            settings["push_interval_quiet_hours"] = self._serialize_interval_quiet_hours(quiet_hours)
            self.user_store.save(state)

        if quiet_hours is None:
            return build_config_updated_message("免打扰时段", "已关闭")
        return build_config_updated_message(
            "免打扰时段",
            f"{format_hhmm(quiet_hours.start)} - {format_hhmm(quiet_hours.end)}",
        )

    def update_threshold(self, identity: UserIdentity, level: str, amount: float) -> str:
        if amount < 0:
            raise UserCommandError("金额不能为负数。")

        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._ensure_user_entry(state, identity)
            settings = self._ensure_settings_mapping(user_entry)
            thresholds = self._read_thresholds(settings)
            updated = UserThresholds(
                warning=amount if level == "warning" else thresholds.warning,
                danger=amount if level == "danger" else thresholds.danger,
                critical=amount if level == "critical" else thresholds.critical,
            )
            self._validate_thresholds(updated)
            threshold_mapping = settings.setdefault("thresholds", {})
            threshold_mapping["warning"] = updated.warning
            threshold_mapping["danger"] = updated.danger
            threshold_mapping["critical"] = updated.critical
            self.user_store.save(state)

        labels = {
            "warning": "警告提醒",
            "danger": "危险提醒",
            "critical": "严重提醒",
        }
        return build_config_updated_message(labels[level], f"余额低于 ${amount:.2f} 时通知")

    def inspect_user(self, open_id: str) -> str:
        with self._user_lock:
            state = self.user_store.load()
            user_entry = self._get_user_entry(state, open_id)
            if user_entry is None or not self._get_key_records(user_entry):
                return build_no_keys_message()
            settings = self._ensure_settings_mapping(user_entry)
            push_time = self._read_push_time(settings)
            push_interval_minutes = self._read_push_interval_minutes(settings)
            push_interval_quiet_hours = self._read_push_interval_quiet_hours(settings)
            key_records = [dict(item) for item in self._get_key_records(user_entry)]

        checked_at = self.now_factory()
        key_sections: list[str] = []
        balance_snapshots = []
        for record in key_records:
            alias = record.get("alias")
            masked_key = mask_api_key(str(record["api_key"]))
            key_metrics: KeyMetrics | None = None
            key_error: str | None = None
            credits = None
            credits_error: str | None = None

            try:
                key_metrics = self.openrouter_client.get_key_metrics(str(record["api_key"]))
            except OpenRouterClientError as exc:
                key_error = exc.message

            try:
                credits = self.openrouter_client.get_credits(str(record["api_key"]))
                if credits is not None:
                    balance = credits.total_credits - credits.total_usage
                    balance_snapshots.append({
                        "key_id": hash_api_key(str(record["api_key"])),
                        "alias": alias,
                        "masked_key": masked_key,
                        "balance": balance,
                        "checked_at": checked_at,
                    })
            except OpenRouterClientError as exc:
                credits_error = exc.message

            key_sections.append(
                build_detail_key_section(
                    alias=alias,
                    masked_key=masked_key,
                    metrics=key_metrics,
                    key_error=key_error,
                    credits=credits,
                    credits_error=credits_error,
                )
            )

        # 记录余额快照
        if balance_snapshots:
            self._record_balance_snapshots(balance_snapshots)

        return build_detail_report(
            checked_at=checked_at,
            key_sections=key_sections,
            push_time=push_time,
            push_interval_minutes=push_interval_minutes,
            interval_quiet_hours=push_interval_quiet_hours,
        )

    def threshold_scan(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            LOGGER.warning("Skipping threshold scan because the previous run is still active.")
            return

        checked_at = self.now_factory()
        LOGGER.info("Starting threshold scan at %s", checked_at.isoformat())
        try:
            users_state = self._load_users_state()
            runtime_state = self._load_runtime_state()
            users = users_state.get("users", {})
            if not isinstance(users, dict):
                return

            for open_id, user_entry in users.items():
                if not isinstance(user_entry, dict):
                    continue
                thresholds = self._read_thresholds(self._ensure_settings_mapping(user_entry))
                for key_record in self._get_key_records(user_entry):
                    self._scan_single_key(
                        open_id=open_id,
                        key_record=key_record,
                        thresholds=thresholds,
                        runtime_state=runtime_state,
                        checked_at=checked_at,
                    )

            self._save_runtime_state(runtime_state)
            LOGGER.info("Completed threshold scan at %s", checked_at.isoformat())
        finally:
            self._scan_lock.release()

    def daily_detail_dispatch(self) -> None:
        if not self._dispatch_lock.acquire(blocking=False):
            LOGGER.warning("Skipping daily detail dispatch because the previous run is still active.")
            return

        now = self.now_factory()
        current_date = now.date().isoformat()
        LOGGER.info("Starting daily detail dispatch at %s", now.isoformat())
        try:
            users_state = self._load_users_state()
            runtime_state = self._load_runtime_state()
            users = users_state.get("users", {})
            if not isinstance(users, dict):
                return

            for open_id, user_entry in users.items():
                if not isinstance(user_entry, dict):
                    continue
                settings = self._ensure_settings_mapping(user_entry)
                if not settings.get("push_enabled", False):
                    continue
                if not self._get_key_records(user_entry):
                    continue

                runtime_user = self._ensure_runtime_user(runtime_state, open_id)
                report: str | None = None

                push_time = self._read_push_time(settings)
                push_interval_quiet_hours = self._read_push_interval_quiet_hours(settings)
                last_daily_push_date = parse_iso_date(runtime_user.get("last_daily_push_date"))
                daily_due = (
                    push_time.hour == now.hour
                    and push_time.minute == now.minute
                    and last_daily_push_date != now.date()
                )
                if daily_due:
                    report = report or self.inspect_user(open_id)
                    sent = self.push_private_text(open_id, report)
                    if sent:
                        runtime_user["last_daily_push_date"] = current_date

                push_interval_minutes = self._read_push_interval_minutes(settings)
                next_interval_push_at = parse_datetime(runtime_user.get("next_interval_push_at"))
                interval_due = (
                    push_interval_minutes is not None
                    and next_interval_push_at is not None
                    and now >= next_interval_push_at
                )
                if interval_due:
                    if self._is_interval_quiet_hours_active(now, push_interval_quiet_hours):
                        runtime_user["next_interval_push_at"] = iso_or_none(
                            self._next_interval_dispatch_after_quiet_hours(now, push_interval_quiet_hours)
                        )
                    else:
                        report = report or self.inspect_user(open_id)
                        sent = self.push_private_text(open_id, report)
                        if sent:
                            runtime_user["next_interval_push_at"] = iso_or_none(
                                self._calculate_next_interval_push_at(now, push_interval_minutes)
                            )

            self._save_runtime_state(runtime_state)
            LOGGER.info("Completed daily detail dispatch at %s", now.isoformat())
        finally:
            self._dispatch_lock.release()

    def push_private_text(self, open_id: str, text: str) -> bool:
        return self.notifier.send_text(text, receive_id=open_id, receive_id_type="open_id")

    def push_text(self, open_id: str, text: str) -> bool:
        return self.push_private_text(open_id, text)

    def push_detail_for_user(self, open_id: str) -> bool:
        resolved_open_id = self.resolve_stored_user_open_id(open_id)
        report = self.inspect_user(resolved_open_id)
        return self.push_private_text(resolved_open_id, report)

    def push_detail_for_all_users(self) -> tuple[int, int]:
        open_ids = self.list_stored_user_open_ids()
        if not open_ids:
            raise UserCommandError("当前还没有任何已绑定用户，无法执行全用户推送。")

        success_count = 0
        for open_id in open_ids:
            if self.push_detail_for_user(open_id):
                success_count += 1
        return success_count, len(open_ids)

    def get_user_trend_report(self, open_id: str) -> str:
        with self._user_lock:
            user_state = self.user_store.load()
            user_entry = self._get_user_entry(user_state, open_id)
            if user_entry is None or not self._get_key_records(user_entry):
                return build_no_keys_message()

        with self._trend_lock:
            trend_state = self.trend_store.load()
            trends = trend_state.get("trends", {})

        user_key_ids = []
        with self._user_lock:
            user_state = self.user_store.load()
            user_entry = self._get_user_entry(user_state, open_id)
            if user_entry:
                for key_record in self._get_key_records(user_entry):
                    key_id = hash_api_key(str(key_record["api_key"]))
                    user_key_ids.append(key_id)

        key_trends = []
        for key_id in user_key_ids:
            key_trend = trends.get(key_id, {})
            if key_trend:
                key_trends.append(key_trend)

        if not key_trends:
            return build_no_keys_message()

        return build_trend_report(key_trends)

    def list_stored_user_open_ids(self) -> list[str]:
        with self._user_lock:
            state = self.user_store.load()
            users = state.get("users", {})
            if not isinstance(users, dict):
                return []

            open_ids: list[str] = []
            for open_id, user_entry in users.items():
                if not isinstance(open_id, str) or not isinstance(user_entry, dict):
                    continue
                if self._get_key_records(user_entry):
                    open_ids.append(open_id)
            return open_ids

    def resolve_stored_user_open_id(self, open_id: str | None = None) -> str:
        if open_id:
            with self._user_lock:
                state = self.user_store.load()
                if self._get_user_entry(state, open_id) is None:
                    raise UserCommandError(f"未找到用户 {open_id} 的绑定记录。")
            return open_id

        open_ids = self.list_stored_user_open_ids()

        if not open_ids:
            raise UserCommandError("当前还没有任何已绑定用户，无法自动选择 open_id。")
        if len(open_ids) > 1:
            raise UserCommandError("已绑定用户不止一个，请在命令行显式指定 --user-open-id。")
        return open_ids[0]

    def _scan_single_key(
        self,
        open_id: str,
        key_record: dict[str, Any],
        thresholds: UserThresholds,
        runtime_state: dict[str, Any],
        checked_at: datetime,
    ) -> None:
        api_key = str(key_record["api_key"])
        alias = key_record.get("alias")
        masked_key = mask_api_key(api_key)
        runtime_key = self._ensure_runtime_key(runtime_state, open_id, str(key_record["key_id"]))

        try:
            metrics = self.openrouter_client.get_key_metrics(api_key)
        except OpenRouterClientError as exc:
            self._handle_failure(
                open_id=open_id,
                runtime_key=runtime_key,
                alias=alias,
                masked_key=masked_key,
                error=exc,
                checked_at=checked_at,
            )
            return

        self._reset_failure_state(runtime_key)
        self._handle_balance_alert(
            open_id=open_id,
            runtime_key=runtime_key,
            alias=alias,
            masked_key=masked_key,
            thresholds=thresholds,
            metrics=metrics,
            checked_at=checked_at,
        )

    def _handle_balance_alert(
        self,
        open_id: str,
        runtime_key: dict[str, Any],
        alias: str | None,
        masked_key: str,
        thresholds: UserThresholds,
        metrics: KeyMetrics,
        checked_at: datetime,
    ) -> None:
        matched_level, matched_amount = self._match_threshold(metrics.limit_remaining, thresholds)
        if matched_level is None or metrics.limit_remaining is None:
            runtime_key["balance_alert"] = None
            return

        balance_state = runtime_key.get("balance_alert")
        if not isinstance(balance_state, dict):
            balance_state = {"level": None, "last_notified_at": None}
        if balance_state.get("level") != matched_level:
            balance_state = {"level": matched_level, "last_notified_at": None}

        last_notified_at = parse_datetime(balance_state.get("last_notified_at"))
        should_notify = dedupe_expired(
            last_notified_at,
            self.config.alerts.balance_dedupe_hours,
            checked_at,
        )
        if should_notify:
            sent = self.push_private_text(
                open_id,
                build_threshold_alert_message(
                    alias=alias,
                    masked_key=masked_key,
                    level=matched_level,
                    threshold_amount=matched_amount,
                    metrics=metrics,
                    checked_at=checked_at,
                ),
            )
            if sent:
                balance_state["last_notified_at"] = iso_or_none(checked_at)

        runtime_key["balance_alert"] = balance_state

    def _record_balance_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        now = self.now_factory()
        seven_days_ago = now - timedelta(days=7)

        with self._trend_lock:
            trend_state = self.trend_store.load()
            trends = trend_state.get("trends", {})

            for snapshot in snapshots:
                key_id = snapshot["key_id"]
                key_trend = trends.setdefault(key_id, {})
                key_trend.setdefault("alias", snapshot["alias"])
                key_trend.setdefault("masked_key", snapshot["masked_key"])
                
                snapshots_list = key_trend.setdefault("snapshots", [])
                snapshots_list.append({
                    "balance": snapshot["balance"],
                    "timestamp": iso_or_none(snapshot["checked_at"]),
                })

                # 清理超过7天的记录
                filtered_snapshots = []
                for snap in snapshots_list:
                    snap_time = parse_datetime(snap["timestamp"])
                    if snap_time and snap_time >= seven_days_ago:
                        filtered_snapshots.append(snap)
                key_trend["snapshots"] = filtered_snapshots

            self.trend_store.save(trend_state)

    def _handle_failure(
        self,
        open_id: str,
        runtime_key: dict[str, Any],
        alias: str | None,
        masked_key: str,
        error: OpenRouterClientError,
        checked_at: datetime,
    ) -> None:
        failure_state = runtime_key.get("failure")
        if not isinstance(failure_state, dict):
            failure_state = {}

        consecutive_failures = int(failure_state.get("consecutive_failures", 0)) + 1
        failure_state["consecutive_failures"] = consecutive_failures
        failure_state["last_error_signature"] = error.kind

        severity = (
            "critical"
            if consecutive_failures >= self.config.alerts.failure.critical_after_failures
            else "error"
        )
        last_signature = failure_state.get("last_notified_signature")
        last_level = failure_state.get("last_notified_level")
        last_notified_at = parse_datetime(failure_state.get("last_notified_at"))
        should_notify = (
            last_signature != error.kind
            or last_level != severity
            or dedupe_expired(last_notified_at, self.config.alerts.failure.dedupe_hours, checked_at)
        )
        if should_notify:
            sent = self.push_private_text(
                open_id,
                build_failure_alert_message(
                    alias=alias,
                    masked_key=masked_key,
                    error_message=error.message,
                    consecutive_failures=consecutive_failures,
                    checked_at=checked_at,
                    critical=severity == "critical",
                ),
            )
            if sent:
                failure_state["last_notified_signature"] = error.kind
                failure_state["last_notified_level"] = severity
                failure_state["last_notified_at"] = iso_or_none(checked_at)

        runtime_key["failure"] = failure_state

    def _match_threshold(self, limit_remaining: float | None, thresholds: UserThresholds) -> tuple[str | None, float]:
        if limit_remaining is None:
            return None, 0.0
        if limit_remaining <= thresholds.critical:
            return "critical", thresholds.critical
        if limit_remaining <= thresholds.danger:
            return "danger", thresholds.danger
        if limit_remaining <= thresholds.warning:
            return "warning", thresholds.warning
        return None, 0.0

    def _reset_failure_state(self, runtime_key: dict[str, Any]) -> None:
        runtime_key["failure"] = {
            "consecutive_failures": 0,
            "last_error_signature": None,
            "last_notified_signature": None,
            "last_notified_level": None,
            "last_notified_at": None,
        }

    def _assert_alias_available(self, keys: list[dict[str, Any]], alias: str | None, key_id: str) -> None:
        if alias is None:
            return
        for item in keys:
            if item.get("key_id") == key_id:
                continue
            if item.get("alias") == alias:
                raise UserCommandError(f"备注名「{alias}」已被占用，请换一个名称。")

    def _match_key_for_delete(self, keys: list[dict[str, Any]], selector: str) -> dict[str, Any]:
        alias_matches = [item for item in keys if item.get("alias") == selector]
        if alias_matches:
            return alias_matches[0]

        full_key_matches = [item for item in keys if str(item["api_key"]) == selector]
        if full_key_matches:
            return full_key_matches[0]

        raise UserCommandError("没有找到匹配的 Key，请使用备注名或完整 Key 删除。")

    def _validate_thresholds(self, thresholds: UserThresholds) -> None:
        if not (thresholds.warning >= thresholds.danger >= thresholds.critical):
            raise UserCommandError("提醒金额必须满足: 警告 >= 危险 >= 严重。")

    def _normalize_alias(self, alias: str | None) -> str | None:
        if alias is None:
            return None
        normalized = alias.strip()
        if not normalized:
            raise UserCommandError("备注名不能为空。")
        return normalized

    def _ensure_user_entry(self, state: dict[str, Any], identity: UserIdentity) -> dict[str, Any]:
        users = self._ensure_users_mapping(state)
        user_entry = users.setdefault(identity.open_id, self._new_user_entry(identity))
        self._merge_identity(user_entry, identity)
        self._ensure_user_defaults(user_entry)
        return user_entry

    def _new_user_entry(self, identity: UserIdentity) -> dict[str, Any]:
        return {
            "identity": {
                "open_id": identity.open_id,
                "user_id": identity.user_id,
                "union_id": identity.union_id,
                "display_name": identity.display_name,
            },
            "settings": self._default_settings(),
            "keys": [],
        }

    def _default_settings(self) -> dict[str, Any]:
        return {
            "push_enabled": False,
            "push_time": format_hhmm(self.config.defaults.push_time),
            "push_interval_minutes": self.config.defaults.push_interval_minutes,
            "push_interval_quiet_hours": self._serialize_interval_quiet_hours(
                self.config.service.interval_quiet_hours
            ),
            "thresholds": {
                "warning": self.config.defaults.thresholds.warning,
                "danger": self.config.defaults.thresholds.danger,
                "critical": self.config.defaults.thresholds.critical,
            },
        }

    def _merge_identity(self, user_entry: dict[str, Any], identity: UserIdentity) -> None:
        identity_mapping = user_entry.setdefault("identity", {})
        identity_mapping["open_id"] = identity.open_id
        if identity.user_id:
            identity_mapping["user_id"] = identity.user_id
        if identity.union_id:
            identity_mapping["union_id"] = identity.union_id
        if identity.display_name:
            identity_mapping["display_name"] = identity.display_name

    def _ensure_user_defaults(self, user_entry: dict[str, Any]) -> None:
        self._ensure_settings_mapping(user_entry)
        if not isinstance(user_entry.get("keys"), list):
            user_entry["keys"] = []

    def _ensure_settings_mapping(self, user_entry: dict[str, Any]) -> dict[str, Any]:
        settings = user_entry.get("settings")
        if not isinstance(settings, dict):
            settings = self._default_settings()
            user_entry["settings"] = settings
        settings.setdefault("push_enabled", False)
        settings.setdefault("push_time", format_hhmm(self.config.defaults.push_time))
        settings.setdefault("push_interval_minutes", None)
        settings.setdefault(
            "push_interval_quiet_hours",
            self._serialize_interval_quiet_hours(self.config.service.interval_quiet_hours),
        )
        threshold_mapping = settings.setdefault("thresholds", {})
        if not isinstance(threshold_mapping, dict):
            threshold_mapping = {}
            settings["thresholds"] = threshold_mapping
        threshold_mapping.setdefault("warning", self.config.defaults.thresholds.warning)
        threshold_mapping.setdefault("danger", self.config.defaults.thresholds.danger)
        threshold_mapping.setdefault("critical", self.config.defaults.thresholds.critical)
        return settings

    def _read_push_time(self, settings: dict[str, Any]) -> time:
        raw = settings.get("push_time", format_hhmm(self.config.defaults.push_time))
        if isinstance(raw, str):
            try:
                return parse_time_value(raw)
            except ValueError:
                return self.config.defaults.push_time
        return self.config.defaults.push_time

    def _read_push_interval_minutes(self, settings: dict[str, Any]) -> int | None:
        if "push_interval_minutes" not in settings:
            return None

        raw = settings.get("push_interval_minutes")
        if raw is None or isinstance(raw, bool):
            return None

        if isinstance(raw, int):
            minutes = raw
        elif isinstance(raw, str):
            try:
                minutes = int(raw.strip())
            except ValueError:
                return None
        else:
            return None

        if minutes <= 0:
            return None
        return minutes

    def _serialize_interval_quiet_hours(
        self,
        quiet_hours: QuietHoursConfig | None,
    ) -> dict[str, str] | None:
        if quiet_hours is None:
            return None
        return {
            "start": format_hhmm(quiet_hours.start),
            "end": format_hhmm(quiet_hours.end),
        }

    def _read_push_interval_quiet_hours(self, settings: dict[str, Any]) -> QuietHoursConfig | None:
        raw = settings.get("push_interval_quiet_hours", self._serialize_interval_quiet_hours(
            self.config.service.interval_quiet_hours
        ))
        if raw is None:
            return None
        if not isinstance(raw, dict):
            return self.config.service.interval_quiet_hours

        try:
            start = parse_time_value(str(raw.get("start", "")).strip())
            end = parse_time_value(str(raw.get("end", "")).strip())
        except ValueError:
            return self.config.service.interval_quiet_hours
        if start == end:
            return self.config.service.interval_quiet_hours
        return QuietHoursConfig(start=start, end=end)

    def _calculate_next_interval_push_at(self, base: datetime, minutes: int) -> datetime:
        aligned_base = base.replace(second=0, microsecond=0)
        return aligned_base + timedelta(minutes=minutes)

    def _is_interval_quiet_hours_active(
        self,
        now: datetime,
        quiet_hours: QuietHoursConfig | None,
    ) -> bool:
        if quiet_hours is None:
            return False

        current = now.timetz().replace(tzinfo=None)
        if quiet_hours.start < quiet_hours.end:
            return quiet_hours.start <= current < quiet_hours.end
        return current >= quiet_hours.start or current < quiet_hours.end

    def _next_interval_dispatch_after_quiet_hours(
        self,
        now: datetime,
        quiet_hours: QuietHoursConfig | None,
    ) -> datetime:
        if quiet_hours is None:
            return now

        current = now.timetz().replace(tzinfo=None)
        if quiet_hours.start < quiet_hours.end:
            resume_date = now.date()
        elif current < quiet_hours.end:
            resume_date = now.date()
        else:
            resume_date = now.date() + timedelta(days=1)

        return datetime.combine(resume_date, quiet_hours.end, tzinfo=self.zoneinfo)

    def _read_thresholds(self, settings: dict[str, Any]) -> UserThresholds:
        raw = settings.get("thresholds")
        if not isinstance(raw, dict):
            return self.config.defaults.thresholds
        try:
            thresholds = UserThresholds(
                warning=float(raw.get("warning", self.config.defaults.thresholds.warning)),
                danger=float(raw.get("danger", self.config.defaults.thresholds.danger)),
                critical=float(raw.get("critical", self.config.defaults.thresholds.critical)),
            )
        except (TypeError, ValueError):
            return self.config.defaults.thresholds
        try:
            self._validate_thresholds(thresholds)
        except UserCommandError:
            return self.config.defaults.thresholds
        return thresholds

    def _get_key_records(self, user_entry: dict[str, Any]) -> list[dict[str, Any]]:
        keys = user_entry.get("keys")
        if not isinstance(keys, list):
            keys = []
            user_entry["keys"] = keys
        invalid_indexes = [index for index, item in enumerate(keys) if not isinstance(item, dict)]
        for index in reversed(invalid_indexes):
            keys.pop(index)
        return keys

    def _ensure_users_mapping(self, state: dict[str, Any]) -> dict[str, Any]:
        users = state.get("users")
        if not isinstance(users, dict):
            users = {}
            state["users"] = users
        return users

    def _get_user_entry(self, state: dict[str, Any], open_id: str) -> dict[str, Any] | None:
        users = state.get("users")
        if not isinstance(users, dict):
            return None
        user_entry = users.get(open_id)
        if not isinstance(user_entry, dict):
            return None
        return user_entry

    def _load_users_state(self) -> dict[str, Any]:
        with self._user_lock:
            return self.user_store.load()

    def _load_runtime_state(self) -> dict[str, Any]:
        with self._runtime_lock:
            return self.runtime_store.load()

    def _save_runtime_state(self, runtime_state: dict[str, Any]) -> None:
        with self._runtime_lock:
            self.runtime_store.save(runtime_state)

    def _get_runtime_user(self, runtime_state: dict[str, Any], open_id: str) -> dict[str, Any] | None:
        users = runtime_state.get("users")
        if not isinstance(users, dict):
            return None
        runtime_user = users.get(open_id)
        if not isinstance(runtime_user, dict):
            return None
        return runtime_user

    def _ensure_runtime_user(self, runtime_state: dict[str, Any], open_id: str) -> dict[str, Any]:
        users = runtime_state.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            runtime_state["users"] = users
        runtime_user = users.get(open_id)
        if not isinstance(runtime_user, dict):
            runtime_user = {"keys": {}, "last_daily_push_date": None, "next_interval_push_at": None}
            users[open_id] = runtime_user
        runtime_user.setdefault("keys", {})
        runtime_user.setdefault("last_daily_push_date", None)
        runtime_user.setdefault("next_interval_push_at", None)
        return runtime_user

    def _ensure_runtime_key(self, runtime_state: dict[str, Any], open_id: str, key_id: str) -> dict[str, Any]:
        runtime_user = self._ensure_runtime_user(runtime_state, open_id)
        runtime_keys = runtime_user.setdefault("keys", {})
        if not isinstance(runtime_keys, dict):
            runtime_keys = {}
            runtime_user["keys"] = runtime_keys
        runtime_key = runtime_keys.get(key_id)
        if not isinstance(runtime_key, dict):
            runtime_key = {"balance_alert": None, "failure": None}
            runtime_keys[key_id] = runtime_key
        return runtime_key

    def _set_next_interval_push_at(self, open_id: str, next_push_at: datetime) -> None:
        with self._runtime_lock:
            runtime_state = self.runtime_store.load()
            runtime_user = self._ensure_runtime_user(runtime_state, open_id)
            runtime_user["next_interval_push_at"] = iso_or_none(next_push_at)
            self.runtime_store.save(runtime_state)

    def _clear_next_interval_push_at(self, open_id: str) -> None:
        with self._runtime_lock:
            runtime_state = self.runtime_store.load()
            runtime_user = self._get_runtime_user(runtime_state, open_id)
            if runtime_user is None:
                return
            runtime_user["next_interval_push_at"] = None
            self.runtime_store.save(runtime_state)

    def _now(self) -> datetime:
        return datetime.now(self.zoneinfo)
