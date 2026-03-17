from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openrouter_monitor.models import BalanceSnapshot
from openrouter_monitor.state_store import SnapshotStore
from openrouter_monitor.utils import calculate_trend_metrics


TZ = ZoneInfo("Asia/Shanghai")


class SnapshotStoreTests(unittest.TestCase):
    def test_record_snapshot_creates_new_entry_for_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            now = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)

            store.record_snapshot("key_1", 100.0, now)

            state = store.load()
            self.assertIn("key_1", state["snapshots"])
            self.assertEqual(len(state["snapshots"]["key_1"]), 1)
            self.assertEqual(state["snapshots"]["key_1"][0]["balance"], 100.0)

    def test_record_snapshot_appends_to_existing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            now1 = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)
            now2 = datetime(2026, 3, 11, 11, 0, tzinfo=TZ)

            store.record_snapshot("key_1", 100.0, now1)
            store.record_snapshot("key_1", 90.0, now2)

            state = store.load()
            self.assertEqual(len(state["snapshots"]["key_1"]), 2)

    def test_record_snapshot_removes_entries_older_than_7_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            old_time = datetime(2026, 3, 1, 10, 0, tzinfo=TZ)
            new_time = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)

            store.record_snapshot("key_1", 100.0, old_time)
            store.record_snapshot("key_1", 80.0, new_time)

            state = store.load()
            self.assertEqual(len(state["snapshots"]["key_1"]), 1)
            self.assertEqual(state["snapshots"]["key_1"][0]["balance"], 80.0)

    def test_get_snapshots_returns_empty_list_for_unknown_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            now = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)

            snapshots = store.get_snapshots("unknown_key", now)

            self.assertEqual(snapshots, [])

    def test_get_snapshots_returns_snapshots_in_descending_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            now1 = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)
            now2 = datetime(2026, 3, 11, 11, 0, tzinfo=TZ)
            now3 = datetime(2026, 3, 11, 12, 0, tzinfo=TZ)

            store.record_snapshot("key_1", 100.0, now1)
            store.record_snapshot("key_1", 90.0, now2)
            store.record_snapshot("key_1", 80.0, now3)

            snapshots = store.get_snapshots("key_1", now3)

            self.assertEqual(len(snapshots), 3)
            self.assertEqual(snapshots[0].balance, 80.0)
            self.assertEqual(snapshots[1].balance, 90.0)
            self.assertEqual(snapshots[2].balance, 100.0)

    def test_get_snapshots_filters_out_old_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            old_time = datetime(2026, 3, 1, 10, 0, tzinfo=TZ)
            new_time = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)

            store.record_snapshot("key_1", 100.0, old_time)
            store.record_snapshot("key_1", 80.0, new_time)

            snapshots = store.get_snapshots("key_1", new_time)

            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].balance, 80.0)

    def test_multiple_keys_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "snapshots.json")
            now = datetime(2026, 3, 11, 10, 0, tzinfo=TZ)

            store.record_snapshot("key_1", 100.0, now)
            store.record_snapshot("key_2", 200.0, now)

            state = store.load()
            self.assertEqual(len(state["snapshots"]), 2)
            self.assertEqual(state["snapshots"]["key_1"][0]["balance"], 100.0)
            self.assertEqual(state["snapshots"]["key_2"][0]["balance"], 200.0)


class TrendMetricsTests(unittest.TestCase):
    def test_calculate_trend_metrics_returns_none_with_less_than_two_snapshots(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertIsNone(daily_consumption)
        self.assertIsNone(estimated_days)

    def test_calculate_trend_metrics_with_decreasing_balance(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
            BalanceSnapshot(key_id="key_1", balance=80.0, timestamp=datetime(2026, 3, 12, 10, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertAlmostEqual(daily_consumption, 20.0)
        self.assertAlmostEqual(estimated_days, 4.0)

    def test_calculate_trend_metrics_with_increasing_balance(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=80.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 12, 10, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertAlmostEqual(daily_consumption, -20.0)
        self.assertIsNone(estimated_days)

    def test_calculate_trend_metrics_with_zero_consumption(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 12, 10, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertAlmostEqual(daily_consumption, 0.0)
        self.assertIsNone(estimated_days)

    def test_calculate_trend_metrics_with_zero_balance(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
            BalanceSnapshot(key_id="key_1", balance=0.0, timestamp=datetime(2026, 3, 12, 10, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertAlmostEqual(daily_consumption, 100.0)
        self.assertAlmostEqual(estimated_days, 0.0)

    def test_calculate_trend_metrics_with_partial_day(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
            BalanceSnapshot(key_id="key_1", balance=70.0, timestamp=datetime(2026, 3, 11, 22, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertAlmostEqual(daily_consumption, 60.0)
        self.assertAlmostEqual(estimated_days, 1.166666, places=5)

    def test_calculate_trend_metrics_handles_unsorted_input(self) -> None:
        snapshots = [
            BalanceSnapshot(key_id="key_1", balance=80.0, timestamp=datetime(2026, 3, 12, 10, 0, tzinfo=TZ)),
            BalanceSnapshot(key_id="key_1", balance=100.0, timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=TZ)),
        ]

        daily_consumption, estimated_days = calculate_trend_metrics(snapshots)

        self.assertAlmostEqual(daily_consumption, 20.0)
        self.assertAlmostEqual(estimated_days, 4.0)


if __name__ == "__main__":
    unittest.main()
