from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from openrouter_monitor import cli
from openrouter_monitor.service import UserCommandError


class FakeService:
    def __init__(self, config: object) -> None:
        self.config = config
        self.threshold_scan_called = 0
        self.inspect_calls: list[str | None] = []
        self.resolve_calls: list[str | None] = []
        self.push_calls: list[tuple[str, str]] = []
        self.push_detail_calls: list[str] = []
        self.push_detail_all_called = 0
        self.push_result = True

    def threshold_scan(self) -> None:
        self.threshold_scan_called += 1

    def resolve_stored_user_open_id(self, open_id: str | None = None) -> str:
        self.resolve_calls.append(open_id)
        if open_id == "missing":
            raise UserCommandError("missing")
        return open_id or "ou_default"

    def inspect_user(self, open_id: str) -> str:
        self.inspect_calls.append(open_id)
        return "inspection-output"

    def push_private_text(self, open_id: str, text: str) -> bool:
        self.push_calls.append((open_id, text))
        return self.push_result

    def push_detail_for_user(self, open_id: str) -> bool:
        self.push_detail_calls.append(open_id)
        return self.push_result

    def push_detail_for_all_users(self) -> tuple[int, int]:
        self.push_detail_all_called += 1
        return (2, 2)


class FakeApp:
    def __init__(self, service: FakeService) -> None:
        self.service = service
        self.run_called = 0

    def run_forever(self) -> None:
        self.run_called += 1


class CliTests(unittest.TestCase):
    def test_main_runs_once_and_prints_completion(self) -> None:
        fake_service = FakeService(config=object())
        stdout = io.StringIO()
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
            redirect_stdout(stdout),
        ):
            result = cli.main(["--config", "config.yaml", "--once"])

        self.assertEqual(result, 0)
        self.assertEqual(fake_service.threshold_scan_called, 1)
        self.assertIn("阈值扫描完成", stdout.getvalue())

    def test_main_runs_inspect_and_prints_report(self) -> None:
        fake_service = FakeService(config=object())
        stdout = io.StringIO()
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
            redirect_stdout(stdout),
        ):
            result = cli.main(["--config", "config.yaml", "--inspect"])

        self.assertEqual(result, 0)
        self.assertEqual(fake_service.resolve_calls, [None])
        self.assertEqual(fake_service.inspect_calls, ["ou_default"])
        self.assertIn("inspection-output", stdout.getvalue())

    def test_main_rejects_once_and_inspect_together(self) -> None:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--config", "config.yaml", "--once", "--inspect"])

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--once, --inspect, --push-text 和 --push-detail", stderr.getvalue())

    def test_main_pushes_text_and_exits(self) -> None:
        fake_service = FakeService(config=object())
        stdout = io.StringIO()
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
            redirect_stdout(stdout),
        ):
            result = cli.main(["--config", "config.yaml", "--push-text", "主动测试消息", "--user-open-id", "ou_123"])

        self.assertEqual(result, 0)
        self.assertEqual(fake_service.push_calls, [("ou_123", "主动测试消息")])
        self.assertIn("主动消息发送成功", stdout.getvalue())

    def test_main_pushes_detail_for_specific_user_and_exits(self) -> None:
        fake_service = FakeService(config=object())
        stdout = io.StringIO()
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
            redirect_stdout(stdout),
        ):
            result = cli.main(["--config", "config.yaml", "--push-detail", "--user-open-id", "ou_123"])

        self.assertEqual(result, 0)
        self.assertEqual(fake_service.resolve_calls, ["ou_123"])
        self.assertEqual(fake_service.push_detail_calls, ["ou_123"])
        self.assertIn("已向用户 ou_123 发送 /详细 主动推送", stdout.getvalue())

    def test_main_pushes_detail_for_all_users_and_exits(self) -> None:
        fake_service = FakeService(config=object())
        stdout = io.StringIO()
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
            redirect_stdout(stdout),
        ):
            result = cli.main(["--config", "config.yaml", "--push-detail", "--all-users"])

        self.assertEqual(result, 0)
        self.assertEqual(fake_service.push_detail_all_called, 1)
        self.assertIn("已向 2/2 个已绑定用户发送 /详细 主动推送", stdout.getvalue())

    def test_main_returns_error_when_user_resolution_fails(self) -> None:
        fake_service = FakeService(config=object())
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
        ):
            result = cli.main(["--config", "config.yaml", "--inspect", "--user-open-id", "missing"])

        self.assertEqual(result, 2)

    def test_main_runs_long_connection_app_by_default(self) -> None:
        fake_service = FakeService(config=object())
        fake_app = FakeApp(fake_service)
        with (
            patch("openrouter_monitor.cli.load_config", return_value=object()),
            patch("openrouter_monitor.cli.MonitorService", return_value=fake_service),
            patch("openrouter_monitor.cli.FeishuLongConnectionApp", return_value=fake_app),
        ):
            result = cli.main(["--config", "config.yaml"])

        self.assertEqual(result, 0)
        self.assertEqual(fake_app.run_called, 1)
