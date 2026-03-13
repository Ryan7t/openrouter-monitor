from __future__ import annotations

from datetime import time
import unittest

from openrouter_monitor.bot import (
    FeishuCommandProcessor,
    IncomingMention,
    IncomingMessage,
    convert_sdk_event,
    extract_command_text,
)
from openrouter_monitor.messages import build_help_message
from openrouter_monitor.models import QuietHoursConfig


class FakeNotifier:
    def __init__(self) -> None:
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
                "reply_to_message_id": reply_to_message_id,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
            }
        )
        return True


class FakeService:
    def __init__(self) -> None:
        self.notifier = FakeNotifier()
        self.config = type("Config", (), {"service": type("Service", (), {"interval_quiet_hours": None})()})()
        self.inspect_calls: list[str] = []
        self.bind_calls: list[tuple[str, str, str | None]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.config_calls: list[tuple[str, str, object]] = []

    def inspect_user(self, open_id: str) -> str:
        self.inspect_calls.append(open_id)
        return "detail-output"

    def bind_key(self, identity: object, api_key: str, alias: str | None = None) -> str:
        self.bind_calls.append((identity.open_id, api_key, alias))
        return "bind-output"

    def delete_key(self, open_id: str, selector: str) -> str:
        self.delete_calls.append((open_id, selector))
        return "delete-output"

    def get_user_config_message(self, open_id: str) -> str:
        self.config_calls.append((open_id, "view", None))
        return "config-output"

    def update_push_time(self, identity: object, push_time: object) -> str:
        self.config_calls.append((identity.open_id, "time", push_time))
        return "time-updated"

    def update_push_interval(self, identity: object, minutes: int | None) -> str:
        self.config_calls.append((identity.open_id, "interval", minutes))
        return "interval-updated"

    def update_push_interval_quiet_hours(self, identity: object, quiet_hours: object) -> str:
        self.config_calls.append((identity.open_id, "interval_quiet_hours", quiet_hours))
        return "interval-quiet-hours-updated"

    def update_threshold(self, identity: object, level: str, amount: float) -> str:
        self.config_calls.append((identity.open_id, level, amount))
        return "threshold-updated"


class FeishuCommandProcessorTests(unittest.TestCase):
    def test_help_message_explains_quiet_hours_only_affect_interval_push(self) -> None:
        message = build_help_message()

        self.assertIn("仅影响间隔推送，不影响每日推送", message)

    def test_group_message_without_mentions_is_ignored(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_123",
                chat_type="group",
                message_type="text",
                content='{"text":"/详细"}',
                mentions=(),
                open_id="ou_1",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.inspect_calls, [])
        self.assertEqual(service.notifier.messages, [])

    def test_detail_command_uses_sender_open_id(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_123",
                chat_type="group",
                message_type="text",
                content='{"text":"@机器人 /详细"}',
                mentions=(IncomingMention(key="@_user_1", name="机器人"),),
                open_id="ou_123",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.inspect_calls, ["ou_123"])
        self.assertEqual(service.notifier.messages[0]["reply_to_message_id"], "om_123")
        self.assertIn("detail-output", service.notifier.messages[0]["text"])

    def test_bind_command_parses_alias(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/绑定 or-v1-abc 别名=生产环境"}',
                mentions=(),
                open_id="ou_456",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.bind_calls, [("ou_456", "or-v1-abc", "生产环境")])
        self.assertIn("bind-output", service.notifier.messages[0]["text"])

    def test_config_push_time_command_updates_personal_setting(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/配置 推送时间 08:30"}',
                mentions=(),
                open_id="ou_456",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.config_calls[0][0], "ou_456")
        self.assertEqual(service.config_calls[0][1], "time")
        self.assertEqual(str(service.config_calls[0][2]), "08:30:00")

    def test_config_interval_command_updates_personal_setting(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/配置 间隔 30"}',
                mentions=(),
                open_id="ou_456",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.config_calls[0], ("ou_456", "interval", 30))

    def test_config_interval_command_can_disable_interval_push(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/配置 间隔 关闭"}',
                mentions=(),
                open_id="ou_456",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.config_calls[0], ("ou_456", "interval", None))

    def test_config_interval_quiet_hours_command_updates_personal_setting(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/配置 间隔静默 23:00 08:00"}',
                mentions=(),
                open_id="ou_456",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.config_calls[0][0], "ou_456")
        self.assertEqual(service.config_calls[0][1], "interval_quiet_hours")
        self.assertEqual(str(service.config_calls[0][2].start), "23:00:00")
        self.assertEqual(str(service.config_calls[0][2].end), "08:00:00")

    def test_config_interval_quiet_hours_command_can_disable_personal_setting(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/配置 间隔静默 关闭"}',
                mentions=(),
                open_id="ou_456",
                user_id="u_1",
                union_id="un_1",
            )
        )

        self.assertEqual(service.config_calls[0], ("ou_456", "interval_quiet_hours", None))

    def test_bind_command_replies_error_when_open_id_is_missing(self) -> None:
        service = FakeService()
        processor = FeishuCommandProcessor(service)

        processor.handle_message(
            IncomingMessage(
                message_id="om_123",
                chat_id="oc_456",
                chat_type="p2p",
                message_type="text",
                content='{"text":"/绑定 or-v1-abc"}',
                mentions=(),
                open_id=None,
                user_id=None,
                union_id=None,
            )
        )

        self.assertIn("open_id", service.notifier.messages[0]["text"])

    def test_extract_command_text_removes_mentions(self) -> None:
        result = extract_command_text(
            '{"text":"@机器人 /详细"}',
            (IncomingMention(key="@_user_1", name="机器人"),),
        )

        self.assertEqual(result, "/详细")


class ConvertSdkEventTests(unittest.TestCase):
    def test_convert_sdk_event_extracts_sender_identity(self) -> None:
        mention = type("Mention", (), {"key": "@_user_1", "name": "机器人"})()
        sender_id = type("SenderId", (), {"open_id": "ou_123", "user_id": "u_123", "union_id": "un_123"})()
        sender = type("Sender", (), {"sender_id": sender_id})()
        sdk_message = type(
            "SdkMessage",
            (),
            {
                "message_id": "om_123",
                "chat_id": "oc_123",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text":"hello"}',
                "mentions": [mention],
            },
        )()
        sdk_event = type("SdkEvent", (), {"message": sdk_message, "sender": sender})()
        sdk_payload = type("SdkPayload", (), {"event": sdk_event})()

        result = convert_sdk_event(sdk_payload)

        self.assertIsNotNone(result)
        self.assertEqual(result.open_id, "ou_123")
        self.assertEqual(result.mentions[0].name, "机器人")
