# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenRouter Monitor 是一个基于 Python 3.12 的飞书长连接应用机器人，用于监控用户的 OpenRouter API Key 余额和状态。支持多用户多 Key 管理、个性化阈值告警、每日定时推送。

## Common Commands

```bash
# 虚拟环境 Python（Windows WSL 路径）
PYTHON=.venv/Scripts/python.exe

# 运行全部测试
$PYTHON -m unittest discover -s tests -v

# 运行单个测试文件
$PYTHON -m unittest tests/test_service.py -v

# 编译检查
$PYTHON -m compileall openrouter_monitor tests

# 本地启动（长连接模式）
$PYTHON -m openrouter_monitor --config config.yaml

# 调试：单次阈值扫描
$PYTHON -m openrouter_monitor --config config.yaml --once

# 调试：查看某用户报告
$PYTHON -m openrouter_monitor --config config.yaml --inspect --user-open-id <open_id>

# 调试：推送给所有用户
$PYTHON -m openrouter_monitor --config config.yaml --push-detail --all-users
```

## Architecture

### 核心分层

```
cli.py              命令行入口，解析参数，支持 --once/--inspect/--push-text/--push-detail 模式
bot.py              飞书长连接事件循环 + 命令解析调度（/绑定、/删除、/详细、/配置、/帮助）
service.py          核心业务逻辑（773行，最复杂）：Key 绑定/删除、阈值扫描、每日推送、告警去重
feishu.py           飞书 API 客户端，速率控制 + 重试 + 文本分块
openrouter_client.py  OpenRouter API 客户端（/api/v1/key, /api/v1/credits），重试 + 退避
config.py           YAML 配置加载与严格验证
models.py           dataclass 数据模型（slots 优化）
state_store.py      JSON 文件持久化，原子写入（.tmp → rename）
messages.py         所有消息模板的格式化
utils.py            Key 哈希/脱敏、金额格式化、时间解析、去重判断
```

### 数据流

1. **启动**：`cli.main()` → 加载配置 → 创建 `MonitorService` → 启动 APScheduler 定时任务 → 进入飞书长连接循环
2. **命令处理**：飞书事件 → `FeishuCommandProcessor` 解析命令 → 调用 `service` 方法 → 生成消息 → 回复用户
3. **阈值扫描**：定时触发 → 遍历所有用户和 Key → 调用 OpenRouter API → 判断是否触发告警（含去重）→ 推送告警消息
4. **每日推送**：每分钟检查 → 匹配用户的 `push_time` → 生成完整报告 → 主动私聊推送

### 存储

本地 JSON 文件，无数据库依赖：
- `data/users.json`：用户数据（身份、Keys、配置）
- `data/runtime_state.json`：运行时状态（告警记录、推送日期）

### 并发安全

`service.py` 中使用 `_user_lock`、`_runtime_lock`、`_scan_lock` 三把锁保证线程安全。

## Key Design Decisions

- **Key 标识**：SHA256(api_key) 作为 key_id，原文存储但消息中自动脱敏
- **告警去重**：同 Key 同级别告警在 `balance_dedupe_hours` 内不重复发送
- **失败升级**：连续失败达 `critical_after_failures` 次后升级为严重告警
- **删除匹配**：支持按别名或脱敏 Key 值模糊匹配，存在歧义时提示用户
- **原子写入**：JSON 通过临时文件 + rename 保证一致性

## Tech Stack

- Python 3.12+, APScheduler 3.10.4, lark-oapi 1.5.3, PyYAML, requests
- 测试：unittest（discover 模式）
- CI：GitHub Actions（运行测试 + 编译检查）
- 部署：Docker（python:3.12-slim）
