from __future__ import annotations

import json
import unittest

from openrouter_monitor.feishu import FeishuAppClient


class FakeResponse:
    def __init__(self, code: int = 0, msg: str = "ok") -> None:
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self.code == 0

    def get_log_id(self) -> str:
        return "log-id"


class FakeMessageApi:
    def __init__(self) -> None:
        self.create_calls: list[object] = []
        self.reply_calls: list[object] = []
        self.create_responses: list[FakeResponse] = [FakeResponse()]
        self.reply_responses: list[FakeResponse] = [FakeResponse()]

    def create(self, request: object) -> FakeResponse:
        self.create_calls.append(request)
        return self.create_responses.pop(0)

    def reply(self, request: object) -> FakeResponse:
        self.reply_calls.append(request)
        return self.reply_responses.pop(0)


class FakeSdkClient:
    def __init__(self, message_api: FakeMessageApi) -> None:
        self.im = type("ImService", (), {"v1": type("V1Service", (), {"message": message_api})()})()


class FeishuAppClientTests(unittest.TestCase):
    def test_send_text_creates_message_with_mention_all(self) -> None:
        message_api = FakeMessageApi()
        client = FeishuAppClient("cli_xxx", "secret_xxx", sdk_client=FakeSdkClient(message_api))

        success = client.send_text("critical alert", mention_all=True, receive_id="oc_123")

        self.assertTrue(success)
        request = message_api.create_calls[0]
        self.assertEqual(request.receive_id_type, "open_id")
        payload = json.loads(request.request_body.content)
        self.assertIn('<at user_id="all">所有人</at>', payload["text"])

    def test_send_text_replies_to_message_when_message_id_is_provided(self) -> None:
        message_api = FakeMessageApi()
        client = FeishuAppClient("cli_xxx", "secret_xxx", sdk_client=FakeSdkClient(message_api))

        success = client.send_text("inspection output", reply_to_message_id="om_123")

        self.assertTrue(success)
        self.assertEqual(len(message_api.create_calls), 0)
        self.assertEqual(message_api.reply_calls[0].message_id, "om_123")

    def test_send_text_splits_long_messages(self) -> None:
        message_api = FakeMessageApi()
        message_api.create_responses = [FakeResponse(), FakeResponse(), FakeResponse()]
        client = FeishuAppClient("cli_xxx", "secret_xxx", sdk_client=FakeSdkClient(message_api))
        client.max_text_length = 10

        success = client.send_text("line-1\nline-2\nline-3", receive_id="oc_123")

        self.assertTrue(success)
        self.assertEqual(len(message_api.create_calls), 3)

    def test_send_text_retries_when_frequency_limited(self) -> None:
        message_api = FakeMessageApi()
        message_api.create_responses = [FakeResponse(code=429, msg="too many requests"), FakeResponse()]
        sleeps: list[float] = []
        client = FeishuAppClient(
            "cli_xxx",
            "secret_xxx",
            sdk_client=FakeSdkClient(message_api),
            sleeper=sleeps.append,
        )

        success = client.send_text("plain alert", receive_id="oc_123")

        self.assertTrue(success)
        self.assertEqual(len(message_api.create_calls), 2)
        self.assertEqual(sleeps, [1.0])

    def test_send_text_returns_false_when_create_fails(self) -> None:
        message_api = FakeMessageApi()
        message_api.create_responses = [FakeResponse(code=99991663, msg="forbidden")]
        client = FeishuAppClient("cli_xxx", "secret_xxx", sdk_client=FakeSdkClient(message_api))

        success = client.send_text("plain alert", receive_id="oc_123")

        self.assertFalse(success)
