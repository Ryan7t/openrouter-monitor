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
    ServiceConfig,
    StateConfig,
    UserIdentity,
    UserThresholds,
)
from openrouter_monitor.openrouter_client import OpenRouterClientError
from openrouter_monitor.service import MonitorService, UserCommandError


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
    def make_config(self, temp_dir: str) -> AppConfig:
        return AppConfig(
            service=ServiceConfig(
                poll_interval_minutes=60,
                timezone="Asia/Shanghai",
            ),
            defaults=DefaultsConfig(
                push_time=time(hour=10, minute=45),
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
            ),
        )

    def make_service(
        self,
        temp_dir: str,
        client: FakeOpenRouterClient | None = None,
        notifier: FakeNotifier | None = None,
        now_values: list[datetime] | None = None,
    ) -> MonitorService:
        return MonitorService(
            config=self.make_config(temp_dir),
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

    def test_update_thresholds_are_isolated_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(temp_dir)
            service.update_threshold(UserIdentity(open_id="ou_1"), "warning", 20.0)

            user_one = service.get_user_config_message("ou_1")
            user_two = service.get_user_config_message("ou_2")

            self.assertIn("余额低于 $20.00 时提醒", user_one)
            self.assertIn("余额低于 $10.00 时提醒", user_two)
