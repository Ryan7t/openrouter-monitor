from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from openrouter_monitor.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def write_config(self, content: str) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "config.yaml"
        path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
        return path

    def test_load_config_applies_defaults(self) -> None:
        path = self.write_config(
            """
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        config = load_config(path)

        self.assertEqual(config.service.poll_interval_minutes, 60)
        self.assertEqual(config.service.timezone, "Asia/Shanghai")
        self.assertIsNone(config.service.interval_quiet_hours)
        self.assertEqual(config.defaults.push_time.hour, 10)
        self.assertEqual(config.defaults.push_time.minute, 45)
        self.assertIsNone(config.defaults.push_interval_minutes)
        self.assertEqual(config.defaults.thresholds.warning, 10.0)
        self.assertEqual(config.state.users_path, "data/users.json")
        self.assertEqual(config.state.runtime_path, "data/runtime_state.json")

    def test_load_config_accepts_interval_quiet_hours(self) -> None:
        path = self.write_config(
            """
            service:
              interval_quiet_hours:
                start: "23:00"
                end: "08:00"
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        config = load_config(path)

        self.assertIsNotNone(config.service.interval_quiet_hours)
        self.assertEqual(config.service.interval_quiet_hours.start.hour, 23)
        self.assertEqual(config.service.interval_quiet_hours.end.hour, 8)

    def test_load_config_accepts_push_interval_minutes(self) -> None:
        path = self.write_config(
            """
            defaults:
              push_interval_minutes: 30
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        config = load_config(path)

        self.assertEqual(config.defaults.push_interval_minutes, 30)

    def test_load_config_rejects_invalid_threshold_order(self) -> None:
        path = self.write_config(
            """
            defaults:
              thresholds:
                warning: 5
                danger: 10
                critical: 1
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        with self.assertRaisesRegex(ConfigError, "warning >= danger >= critical"):
            load_config(path)

    def test_load_config_requires_app_id(self) -> None:
        path = self.write_config(
            """
            feishu: {}
            """
        )

        with self.assertRaisesRegex(ConfigError, "feishu.app_id"):
            load_config(path)

    def test_load_config_rejects_invalid_push_time(self) -> None:
        path = self.write_config(
            """
            defaults:
              push_time: "25:00"
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        with self.assertRaisesRegex(ConfigError, "valid 24-hour time"):
            load_config(path)

    def test_load_config_rejects_invalid_push_interval_minutes(self) -> None:
        invalid_values = ["0", "-5", '"abc"']
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                path = self.write_config(
                    f"""
                    defaults:
                      push_interval_minutes: {invalid_value}
                    feishu:
                      app_id: cli_xxx
                      app_secret: secret_xxx
                    """
                )

                with self.assertRaisesRegex(ConfigError, "defaults.push_interval_minutes"):
                    load_config(path)

    def test_load_config_rejects_invalid_interval_quiet_hours(self) -> None:
        path = self.write_config(
            """
            service:
              interval_quiet_hours:
                start: "23:00"
                end: "23:00"
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        with self.assertRaisesRegex(ConfigError, "service.interval_quiet_hours"):
            load_config(path)

    def test_load_config_rejects_invalid_failure_settings(self) -> None:
        path = self.write_config(
            """
            alerts:
              failure:
                critical_after_failures: 0
            feishu:
              app_id: cli_xxx
              app_secret: secret_xxx
            """
        )

        with self.assertRaisesRegex(ConfigError, "critical_after_failures"):
            load_config(path)
