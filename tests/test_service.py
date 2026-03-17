from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openrouter_monitor.models import (
    AlertsConfig,
    AppConfig,
    DefaultsConfig,
    FailureAlertConfig,
    FeishuConfig,
    KeyMetrics,
    QuietHoursConfig,
    ServiceConfig,
    StateConfig,
    UserIdentity,
    UserThresholds,
)
from openrouter_monitor.openrouter_client import OpenRouterClientError
from openrouter_monitor.service import MonitorService, SCHEDULER_MISFIRE_GRACE_SECONDS, UserCommandError


TZ = ZoneInfo("Asia/Shanghai")


def build_metrics(limit_remaining: float | None, label: str = "prod-label") -> KeyMetrics:
    return KeyMetrics(
        label=label,
        is_free_tier=False,
        is_management_key=False,
        is_provisioning_key=False,
        usage=12.5,
        limit=20.0,
        limit_remaining=limit_remaining,
        limit_reset=None,
        expires_at=None,
        usage_daily=1.5,
        usage_weekly=3.0,
        usage_monthly=12.5,
        include_byok_in_limit=False,
        byok_usage=0.0,
        byok_usage_daily=0.0,
        byok_usage_weekly=0.0,
        byok_usage_monthly=0.0,
        rate_limit_requests=200,
        rate_limit_interval="10s",
        rate_limit_note="legacy",
    )


class FakeCredits:
    def __init__(self, total_credits: float, total_usage: float) -> None:
        self.total_credits = total_credits
        self.total_usage = total_usage

    @property
    def remaining(self) -> float:
        return self.total_credits - self.total_usage


class FakeOpenRouterClient:
    def __init__(self) -> None:
        self.key_responses: dict[str, list[object]] = {}
        self.credits_responses: dict[str, list[object]] = {}

    def get_key_metrics(self, api_key: str) -> KeyMetrics:
        response = self.key_responses[api_key].pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get_credits(self, api_key: str) -> FakeCredits:
        response = self.credits_responses[api_key].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeNotifier:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.messages: list[dict[str, object]] = []

    def send_text(
        self,
        text: str,
        mention_all: bool = False,
        receive_id: str | None = None,
        receive_id_type: str = "chat_id",
        reply_to_message_id: str | None = None,
        reply_in_thread: bool = False,
    ) -> bool:
        self.messages.append(
            {
                "text": text,
                "mention_all": mention_all,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        if self.outcomes:
            return self.outcomes.pop(0)
        return True


class Clock:
    def __init__(self, values: list[datetime]) -> None:
        self.values = list(values)
        self.last_value = values[-1]

    def now(self) -> datetime:
        if self.values:
            self.last_value = self.values.pop(0)
        return self.last_value


class MonitorServiceTests(unittest.TestCase):
    def make_config(self, temp_dir: str, interval_quiet_hours: QuietHoursConfig | None = None) -> AppConfig:
        return AppConfig(
            service=ServiceConfig(
                poll_interval_minutes=60,
                timezone="Asia/Shanghai",
                interval_quiet_hours=interval_quiet_hours,
            ),
            defaults=DefaultsConfig(
                push_time=time(hour=10, minute=45),
                push_interval_minutes=None,
                thresholds=UserThresholds(warning=10.0, danger=5.0, critical=1.0),
            ),
            alerts=AlertsConfig(
                balance_dedupe_hours=24,
                failure=FailureAlertConfig(dedupe_hours=24, critical_after_failures=3),
            ),
            feishu=FeishuConfig(
                app_id="cli_xxx",
                app_secret="secret_xxx",
            ),
            state=StateConfig(
                users_path=str(Path(temp_dir) / "users.json"),
                runtime_path=str(Path(temp_dir) / "runtime.json"),
                snapshots_path=str(Path(temp_dir) / "snapshots.json"),
            ),
        )

    def make_service(
        self,
        temp_dir: str,
        client: FakeOpenRouterClient | None = None,
        notifier: FakeNotifier | None = None,
        now_values: list[datetime] | None = None,
        interval_quiet_hours: QuietHoursConfig | None = None,
    ) -> MonitorService:
        return MonitorService(
            config=self.make_config(temp_dir, interval_quiet_hours=interval_quiet_hours),
            openrouter_client=client or FakeOpenRouterClient(),
            notifier=notifier or FakeNotifier(),
            now_factory=Clock(now_values or [datetime(2026, 3, 11, 10, 0, tzinfo=TZ)]).now,
        )

    def test_bind_key_is_idempotent_and_updates_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(
                temp_dir,
                now_values=[
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 5, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1", user_id="u_1")

            first_message = service.bind_key(identity, "or-v1-abc", "生产")
            second_message = service.bind_key(identity, "or-v1-abc", "主生产")

            self.assertIn("绑定成功", first_message)
            self.assertIn("更新成功", second_message)
            report = service.get_user_config_message("ou_1")
            self.assertIn("已绑定 Key: 1 个", report)

    def test_bind_key_rejects_duplicate_alias_within_same_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(temp_dir)
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            with self.assertRaisesRegex(UserCommandError, "备注名「生产」已被占用"):
                service.bind_key(identity, "or-v1-def", "生产")

    def test_delete_key_removes_runtime_state_and_disables_push_when_last_key_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(0.5)]
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[datetime(2026, 3, 11, 10, 0, tzinfo=TZ)],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.threshold_scan()

            message = service.delete_key("ou_1", "生产")

            self.assertIn("每日推送已自动关闭", message)
            self.assertEqual(service.runtime_store.load()["users"], {})

    def test_delete_key_supports_full_key_when_alias_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(temp_dir)
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-no-alias")

            message = service.delete_key("ou_1", "or-v1-no-alias")

            self.assertIn("删除成功", message)
            self.assertIn("当前已无绑定的 Key", message)

    def test_inspect_user_includes_all_keys_and_masks_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abcdef1234567890xyz"] = [build_metrics(8.0, label="prod")]
            client.key_responses["or-v1-second1234567890xyz"] = [build_metrics(None, label="staging")]
            client.credits_responses["or-v1-abcdef1234567890xyz"] = [FakeCredits(100.0, 76.5)]
            client.credits_responses["or-v1-second1234567890xyz"] = [
                OpenRouterClientError("forbidden", "No credits permission")
            ]
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[datetime(2026, 3, 11, 10, 0, tzinfo=TZ)],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abcdef1234567890xyz", "生产")
            service.bind_key(identity, "or-v1-second1234567890xyz", "预发")

            report = service.inspect_user("ou_1")

            self.assertIn("【生产】", report)
            self.assertIn("【预发】", report)
            self.assertNotIn("or-v1-abcdef1234567890xyz", report)
            self.assertIn("No credits permission", report)

    def test_threshold_scan_dedupes_alerts_per_user_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(8.0), build_metrics(8.0), build_metrics(8.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 11, 0, tzinfo=TZ),
                    datetime(2026, 3, 12, 11, 1, tzinfo=TZ),
                ],
            )
            service.bind_key(UserIdentity(open_id="ou_1"), "or-v1-abc", "生产")

            service.threshold_scan()
            service.threshold_scan()
            service.threshold_scan()

            self.assertEqual(len(notifier.messages), 2)
            self.assertTrue(all(item["receive_id"] == "ou_1" for item in notifier.messages))
            self.assertIn("余额不足提醒", notifier.messages[0]["text"])

    def test_threshold_scan_failure_escalates_after_configured_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            error = OpenRouterClientError("network_error", "timeout")
            client.key_responses["or-v1-abc"] = [error, error, error]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 11, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 12, 0, tzinfo=TZ),
                ],
            )
            service.bind_key(UserIdentity(open_id="ou_1"), "or-v1-abc", "生产")

            service.threshold_scan()
            service.threshold_scan()
            service.threshold_scan()

            self.assertEqual(len(notifier.messages), 2)
            self.assertIn("【异常】", notifier.messages[0]["text"])
            self.assertIn("【紧急】", notifier.messages[1]["text"])

    def test_daily_detail_dispatch_sends_once_per_day_at_personal_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0), build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0), FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 12, 9, 0, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_time(identity, time(hour=9, minute=0))

            service.daily_detail_dispatch()
            service.daily_detail_dispatch()
            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 2)
            self.assertTrue(all(item["receive_id_type"] == "open_id" for item in notifier.messages))
            self.assertIn("OpenRouter 余额报告", notifier.messages[0]["text"])

    def test_start_scheduler_configures_misfire_grace_time_for_background_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(temp_dir)

            try:
                service.start_scheduler()

                self.assertIsNotNone(service._scheduler)
                threshold_job = service._scheduler.get_job("threshold-scan")
                dispatch_job = service._scheduler.get_job("daily-detail-dispatch")

                self.assertIsNotNone(threshold_job)
                self.assertIsNotNone(dispatch_job)
                self.assertEqual(threshold_job.misfire_grace_time, SCHEDULER_MISFIRE_GRACE_SECONDS)
                self.assertEqual(dispatch_job.misfire_grace_time, SCHEDULER_MISFIRE_GRACE_SECONDS)
            finally:
                service.stop_scheduler()

    def test_push_detail_for_user_does_not_change_daily_dispatch_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[datetime(2026, 3, 11, 9, 0, tzinfo=TZ)],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            success = service.push_detail_for_user("ou_1")

            self.assertTrue(success)
            self.assertEqual(notifier.messages[0]["receive_id"], "ou_1")
            self.assertEqual(service.runtime_store.load()["users"], {})

    def test_push_detail_for_user_does_not_change_interval_dispatch_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 5, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 6, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 30)
            before_push_detail = service.runtime_store.load()["users"]["ou_1"]["next_interval_push_at"]

            success = service.push_detail_for_user("ou_1")

            self.assertTrue(success)
            self.assertEqual(
                service.runtime_store.load()["users"]["ou_1"]["next_interval_push_at"],
                before_push_detail,
            )

    def test_interval_dispatch_starts_from_update_time_and_repeats_by_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [
                build_metrics(12.0),
                build_metrics(12.0),
            ]
            client.credits_responses["or-v1-abc"] = [
                FakeCredits(100.0, 50.0),
                FakeCredits(100.0, 50.0),
            ]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 5, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 34, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 35, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 35, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 4, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 5, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 5, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 30)

            service.daily_detail_dispatch()
            service.daily_detail_dispatch()
            service.daily_detail_dispatch()
            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 2)
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 11, 10, 35, tzinfo=TZ),
            )

    def test_one_minute_interval_stays_minute_aligned_when_dispatch_runs_seconds_late(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [
                build_metrics(12.0),
                build_metrics(12.0),
            ]
            client.credits_responses["or-v1-abc"] = [
                FakeCredits(100.0, 50.0),
                FakeCredits(100.0, 50.0),
            ]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, 2, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 0, 2, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 1, 2, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 1, 2, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 2, 2, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 2, 2, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 1)

            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 11, 9, 1, 0, tzinfo=TZ),
            )

            service.daily_detail_dispatch()
            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 2)
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 11, 9, 3, 0, tzinfo=TZ),
            )

    def test_interval_dispatch_skips_during_personal_quiet_hours_and_resumes_after_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 22, 55, tzinfo=TZ),
                    datetime(2026, 3, 11, 22, 55, tzinfo=TZ),
                    datetime(2026, 3, 11, 23, 10, tzinfo=TZ),
                    datetime(2026, 3, 12, 8, 0, tzinfo=TZ),
                    datetime(2026, 3, 12, 8, 0, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 15)
            service.update_push_interval_quiet_hours(
                identity,
                QuietHoursConfig(start=time(hour=23, minute=0), end=time(hour=8, minute=0)),
            )

            service.daily_detail_dispatch()
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(notifier.messages, [])
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 12, 8, 0, tzinfo=TZ),
            )

            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 1)
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 12, 8, 15, tzinfo=TZ),
            )

    def test_daily_push_is_not_blocked_by_personal_interval_quiet_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 22, 50, tzinfo=TZ),
                    datetime(2026, 3, 11, 23, 5, tzinfo=TZ),
                    datetime(2026, 3, 11, 23, 5, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_time(identity, time(hour=23, minute=5))
            service.update_push_interval_quiet_hours(
                identity,
                QuietHoursConfig(start=time(hour=23, minute=0), end=time(hour=8, minute=0)),
            )

            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 1)
            self.assertIn("OpenRouter 余额报告", notifier.messages[0]["text"])

    def test_get_user_config_message_displays_default_interval_quiet_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(
                temp_dir,
                interval_quiet_hours=QuietHoursConfig(start=time(hour=23, minute=0), end=time(hour=8, minute=0)),
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 30)

            message = service.get_user_config_message("ou_1")

            self.assertIn("免打扰时段: 23:00 - 08:00", message)

    def test_get_user_config_message_hides_quiet_hours_when_interval_push_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(
                temp_dir,
                interval_quiet_hours=QuietHoursConfig(start=time(hour=23, minute=0), end=time(hour=8, minute=0)),
            )

            message = service.get_user_config_message("ou_1")

            self.assertNotIn("免打扰时段", message)

    def test_user_can_disable_default_interval_quiet_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 22, 55, tzinfo=TZ),
                    datetime(2026, 3, 11, 22, 55, tzinfo=TZ),
                    datetime(2026, 3, 11, 23, 10, tzinfo=TZ),
                    datetime(2026, 3, 11, 23, 10, tzinfo=TZ),
                ],
                interval_quiet_hours=QuietHoursConfig(start=time(hour=23, minute=0), end=time(hour=8, minute=0)),
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 15)
            service.update_push_interval_quiet_hours(identity, None)

            message = service.get_user_config_message("ou_1")
            service.daily_detail_dispatch()

            self.assertIn("免打扰时段: 未开启", message)
            self.assertEqual(len(notifier.messages), 1)
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 11, 23, 25, tzinfo=TZ),
            )

    def test_update_push_interval_can_disable_interval_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 5, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 30)

            message = service.update_push_interval(identity, None)

            self.assertIn("间隔推送", message)
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertIsNone(runtime_user["next_interval_push_at"])
            service.daily_detail_dispatch()
            self.assertEqual(notifier.messages, [])

    def test_interval_and_daily_dispatch_can_both_send_in_same_minute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_time(identity, time(hour=10, minute=0))
            service.update_push_interval(identity, 60)

            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 2)
            self.assertTrue(all(item["text"] == notifier.messages[0]["text"] for item in notifier.messages))

    def test_interval_dispatch_retries_after_failure_without_advancing_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [
                build_metrics(12.0),
                build_metrics(12.0),
            ]
            client.credits_responses["or-v1-abc"] = [
                FakeCredits(100.0, 50.0),
                FakeCredits(100.0, 50.0),
            ]
            notifier = FakeNotifier(outcomes=[False, True])
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 1, tzinfo=TZ),
                    datetime(2026, 3, 11, 10, 1, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")
            service.update_push_interval(identity, 60)

            service.daily_detail_dispatch()
            failed_schedule = service.runtime_store.load()["users"]["ou_1"]["next_interval_push_at"]
            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 2)
            self.assertEqual(
                datetime.fromisoformat(failed_schedule),
                datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
            )
            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 11, 11, 1, tzinfo=TZ),
            )

    def test_interval_schedule_starts_when_first_key_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            notifier = FakeNotifier()
            service = self.make_service(
                temp_dir,
                client=client,
                notifier=notifier,
                now_values=[
                    datetime(2026, 3, 11, 9, 0, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 10, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 39, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 40, tzinfo=TZ),
                    datetime(2026, 3, 11, 9, 40, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.update_push_interval(identity, 30)
            service.bind_key(identity, "or-v1-abc", "生产")

            runtime_user = service.runtime_store.load()["users"]["ou_1"]
            self.assertEqual(
                datetime.fromisoformat(runtime_user["next_interval_push_at"]),
                datetime(2026, 3, 11, 9, 40, tzinfo=TZ),
            )

            service.daily_detail_dispatch()
            service.daily_detail_dispatch()

            self.assertEqual(len(notifier.messages), 1)

    def test_update_thresholds_are_isolated_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(temp_dir)
            service.update_threshold(UserIdentity(open_id="ou_1"), "warning", 20.0)

            user_one = service.get_user_config_message("ou_1")
            user_two = service.get_user_config_message("ou_2")

            self.assertIn("余额低于 $20.00 时提醒", user_one)
            self.assertIn("余额低于 $10.00 时提醒", user_two)

    def test_inspect_user_records_balance_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 30.0)]
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[datetime(2026, 3, 11, 10, 0, tzinfo=TZ)],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            service.inspect_user("ou_1")

            snapshot_state = service.snapshot_store.load()
            key_id = service.user_store.load()["users"]["ou_1"]["keys"][0]["key_id"]
            self.assertIn(key_id, snapshot_state.get("snapshots", {}))
            key_snapshots = snapshot_state["snapshots"][key_id]
            self.assertEqual(len(key_snapshots), 1)
            self.assertEqual(key_snapshots[0]["balance"], 70.0)

    def test_inspect_user_does_not_record_snapshot_when_credits_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [OpenRouterClientError("forbidden", "No permission")]
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[datetime(2026, 3, 11, 10, 0, tzinfo=TZ)],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            service.inspect_user("ou_1")

            snapshot_state = service.snapshot_store.load()
            self.assertEqual(snapshot_state.get("snapshots", {}), {})

    def test_get_user_trend_returns_no_keys_message_when_user_has_no_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(temp_dir)

            message = service.get_user_trend("ou_1")

            self.assertIn("还没有绑定任何 Key", message)

    def test_get_user_trend_shows_current_balance_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [FakeCredits(100.0, 50.0)]
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[datetime(2026, 3, 11, 10, 0, tzinfo=TZ)],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            message = service.get_user_trend("ou_1")

            self.assertIn("OpenRouter 余额趋势报告", message)
            self.assertIn("【生产】", message)
            self.assertIn("当前余额: $50.00", message)
            self.assertIn("暂无历史记录", message)

    def test_get_user_trend_calculates_daily_consumption_and_estimated_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [build_metrics(12.0), build_metrics(12.0)]
            client.credits_responses["or-v1-abc"] = [
                FakeCredits(100.0, 70.0),  # Day 1: balance = 30
                FakeCredits(100.0, 40.0),  # Day 2: balance = 60
                FakeCredits(100.0, 40.0),  # Day 3: for get_user_trend
            ]
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[
                    datetime(2026, 3, 11, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 12, 10, 0, tzinfo=TZ),
                    datetime(2026, 3, 13, 10, 0, tzinfo=TZ),
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            service.inspect_user("ou_1")
            service.inspect_user("ou_1")

            message = service.get_user_trend("ou_1")

            # Balance went from 30 to 60 over 1 day = growth of 30/day
            self.assertIn("日均增长: $30.00", message)
            self.assertIn("预计可用: 余额在增长或持平，无法估算", message)

    def test_snapshot_cleanup_removes_records_older_than_7_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeOpenRouterClient()
            client.key_responses["or-v1-abc"] = [
                build_metrics(12.0),  # Day 1
                build_metrics(12.0),  # Day 10 (cleanup happens here)
            ]
            client.credits_responses["or-v1-abc"] = [
                FakeCredits(100.0, 10.0),  # Day 1: balance = 90
                FakeCredits(100.0, 20.0),  # Day 10: balance = 80
            ]
            # Use naive datetime to match service.now_factory() behavior
            service = self.make_service(
                temp_dir,
                client=client,
                now_values=[
                    datetime(2026, 3, 1, 10, 0),   # bind_key
                    datetime(2026, 3, 1, 10, 0),   # Day 1
                    datetime(2026, 3, 10, 10, 0),  # Day 10 (> 7 days later)
                ],
            )
            identity = UserIdentity(open_id="ou_1")
            service.bind_key(identity, "or-v1-abc", "生产")

            # First call at Day 1 records a snapshot
            service.inspect_user("ou_1")
            # Verify snapshot was recorded
            key_id = service.user_store.load()["users"]["ou_1"]["keys"][0]["key_id"]
            day1 = datetime(2026, 3, 1, 10, 0)
            snapshots = service.snapshot_store.get_snapshots(key_id, day1)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].balance, 90.0)

            # Second call at Day 10 records another snapshot and cleans up Day 1
            service.inspect_user("ou_1")

            # Verify that when we get snapshots at Day 10, only the new one remains
            day10 = datetime(2026, 3, 10, 10, 0)
            snapshots = service.snapshot_store.get_snapshots(key_id, day10)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].balance, 80.0)
