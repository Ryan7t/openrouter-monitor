from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from openrouter_monitor.models import AppConfig, StateConfig
from openrouter_monitor.service import MonitorService
from openrouter_monitor.state_store import BalanceTrendStore


class TestBalanceTrend:
    def test_record_balance_snapshots(self, tmp_path):
        # 创建临时测试文件
        trend_file = tmp_path / "balance_trends.json"
        
        # 配置
        config = AppConfig(
            service=Mock(timezone="Asia/Shanghai"),
            defaults=Mock(),
            alerts=Mock(),
            feishu=Mock(),
            state=StateConfig(
                users_path=str(tmp_path / "users.json"),
                runtime_path=str(tmp_path / "runtime_state.json"),
                trends_path=str(trend_file),
            ),
        )
        
        # 创建服务实例
        service = MonitorService(config)
        
        # 模拟时间
        now = datetime(2024, 1, 1, 10, 0, 0)
        with patch.object(service, 'now_factory', return_value=now):
            # 记录快照
            service._record_balance_snapshots([
                {
                    "key_id": "test_key_id",
                    "alias": "Test Key",
                    "masked_key": "sk-or-v1-xxx",
                    "balance": 100.0,
                    "checked_at": now,
                }
            ])
        
        # 验证数据存储
        trend_store = BalanceTrendStore(str(trend_file))
        trend_state = trend_store.load()
        assert "test_key_id" in trend_state["trends"]
        key_trend = trend_state["trends"]["test_key_id"]
        assert key_trend["alias"] == "Test Key"
        assert key_trend["masked_key"] == "sk-or-v1-xxx"
        assert len(key_trend["snapshots"]) == 1
        assert key_trend["snapshots"][0]["balance"] == 100.0
        
    def test_snapshot_cleanup(self, tmp_path):
        # 创建临时测试文件
        trend_file = tmp_path / "balance_trends.json"
        
        # 配置
        config = AppConfig(
            service=Mock(timezone="Asia/Shanghai"),
            defaults=Mock(),
            alerts=Mock(),
            feishu=Mock(),
            state=StateConfig(
                users_path=str(tmp_path / "users.json"),
                runtime_path=str(tmp_path / "runtime_state.json"),
                trends_path=str(trend_file),
            ),
        )
        
        # 创建服务实例
        service = MonitorService(config)
        
        # 记录超过7天的快照
        now = datetime(2024, 1, 1, 10, 0, 0)
        eight_days_ago = now - timedelta(days=8)
        
        with patch.object(service, 'now_factory', return_value=now):
            # 先记录旧快照
            service._record_balance_snapshots([
                {
                    "key_id": "test_key_id",
                    "alias": "Test Key",
                    "masked_key": "sk-or-v1-xxx",
                    "balance": 100.0,
                    "checked_at": eight_days_ago,
                }
            ])
            
            # 再记录新快照
            service._record_balance_snapshots([
                {
                    "key_id": "test_key_id",
                    "alias": "Test Key",
                    "masked_key": "sk-or-v1-xxx",
                    "balance": 90.0,
                    "checked_at": now,
                }
            ])
        
        # 验证旧快照被清理
        trend_store = BalanceTrendStore(str(trend_file))
        trend_state = trend_store.load()
        key_trend = trend_state["trends"]["test_key_id"]
        assert len(key_trend["snapshots"]) == 1
        assert key_trend["snapshots"][0]["balance"] == 90.0


if __name__ == "__main__":
    pytest.main([__file__])