# OpenRouter Monitor

一个基于 Python 3.12 的 OpenRouter 飞书机器人，按用户 `open_id` 隔离管理多个 Key，支持长连接收消息、个人化阈值配置、每日主动私聊推送和阈值告警。

## 特性

- 飞书应用机器人 + 长连接，不需要公网回调地址
- 用户通过 `@机器人` 指令自助绑定和删除 OpenRouter Key
- 单用户可绑定多个 Key，并可设置别名
- 每个用户有独立的推送时间和 `warning/danger/critical` 阈值
- 每日主动私聊推送 `/详细` 完整报告
- 定时阈值扫描，命中后主动私聊告警
- 本地 JSON 存储，无 SQLite / MySQL / PostgreSQL
- 所有消息中的 Key 都会脱敏显示

## 架构

- `data/users.json`
  - 存储用户身份、个人配置、已绑定 Key
- `data/runtime_state.json`
  - 存储告警去重状态、失败计数、每日推送日期
- `openrouter_monitor/service.py`
  - 用户绑定、删除、配置更新、阈值扫描、每日推送
- `openrouter_monitor/bot.py`
  - 飞书长连接事件接入与中文指令处理

## 运行要求

- Python 3.12
- 必须使用仓库现有虚拟环境 `.venv`
- 本地开发和测试不要使用全局 Python

Windows 下所有命令请使用：

```powershell
.\.venv\Scripts\python.exe
```

## 安装

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

## 飞书接入

当前项目使用的是飞书开放平台自建应用机器人，不是群自定义机器人 Webhook。

需要在飞书开放平台完成：

1. 创建自建应用
2. 添加机器人能力
3. 在“事件与回调”中选择“长连接接收事件”
4. 订阅 `p2.im.message.receive_v1`
5. 开通机器人收发消息所需 IM 权限
6. 发布应用，并将机器人添加到测试群或私聊场景

官方文档：

- 飞书发送消息接口：https://open.feishu.cn/document/server-docs/im-v1/message/create
- 飞书接收消息事件：https://open.feishu.cn/document/server-docs/im-v1/message/events/receive

## 配置

复制示例配置：

```powershell
Copy-Item config.example.yaml config.yaml
```

最小配置示例：

```yaml
service:
  poll_interval_minutes: 60
  timezone: Asia/Shanghai

defaults:
  push_time: "09:00"
  thresholds:
    warning: 10
    danger: 5
    critical: 1

alerts:
  balance_dedupe_hours: 24
  failure:
    dedupe_hours: 24
    critical_after_failures: 3

feishu:
  app_id: cli_xxxxxxxxxxxxxxxx
  app_secret: your-feishu-app-secret

state:
  users_path: data/users.json
  runtime_path: data/runtime_state.json
```

说明：

- 这里不再配置 `openrouter.keys`
- Key 由飞书用户通过 `/绑定` 动态写入后端
- 升级到当前版本后，旧全局 Key 不会自动迁移，用户需要重新绑定

## 启动

```powershell
.\.venv\Scripts\python.exe -m openrouter_monitor --config config.yaml
```

或：

```powershell
.\.venv\Scripts\openrouter-monitor.exe --config config.yaml
```

启动后会：

- 建立飞书长连接
- 立即执行一次阈值扫描
- 按配置启动后台定时任务

## 飞书指令

群聊里请先 `@机器人`，私聊机器人时可直接输入。

### 查询指令

- `/详细`
  - 返回当前用户已绑定的全部 Key 报告
  - 每个 Key 会独立调用：
    - `GET /api/v1/key`
    - `GET /api/v1/credits`

### 管理指令

- `/绑定 <key> [别名=名称]`
  - 例：`/绑定 or-v1-xxxxx`
  - 例：`/绑定 or-v1-xxxxx 别名=生产环境`
  - 绑定成功后自动启用该用户的每日推送

- `/删除 <别名或完整Key>`
  - 删除某一个已绑定 Key
  - 优先按别名匹配；如果没有别名，也可以直接传完整 Key 删除

- `/配置 查看`
  - 查看当前个人配置

- `/配置 推送时间 HH:MM`
  - 修改个人每日主动推送时间

- `/配置 警告 <amount>`
- `/配置 危险 <amount>`
- `/配置 严重 <amount>`
  - 修改当前用户的个人阈值

- `/帮助`

英文兼容别名：

- `/inspect`
- `/bind`
- `/delete`
- `/config`
- `/help`

## 主动推送行为

### 每日主动推送

- 每个用户都有独立的 `push_time`
- 到点后，机器人会主动私聊该用户，发送 `/详细` 的完整内容
- 同一天只发送一次

### 阈值告警

- 后台按 `service.poll_interval_minutes` 扫描全部用户的全部 Key
- 阈值判断使用用户自己的 `warning/danger/critical`
- 告警去重使用全局 `alerts.balance_dedupe_hours`

### 失败告警

- `/api/v1/key` 调用失败会累计连续失败次数
- 连续失败达到 `alerts.failure.critical_after_failures` 时升级为严重错误
- 失败告警按 `用户 + key` 维度去重

## `alerts` 配置怎么理解

这段配置控制的不是“余额多少时告警”，而是“告警发出去以后，多久可以再发一次”和“连续失败多少次要升级”。

```yaml
alerts:
  balance_dedupe_hours: 24
  failure:
    dedupe_hours: 24
    critical_after_failures: 3
```

- `balance_dedupe_hours`
  - 同一个用户的同一个 Key 如果已经发过一次额度告警，那么至少过 `24` 小时，才会再次发送同级别额度告警
  - 作用是防止每天轮询时反复刷屏

- `failure.dedupe_hours`
  - 同一个用户的同一个 Key 如果持续报同一种错误，也至少隔 `24` 小时才重复提醒一次
  - 作用是防止接口连续失败时不停刷错误消息

- `failure.critical_after_failures`
  - 同一个用户的同一个 Key 连续失败达到 `3` 次后，错误级别会从普通错误升级为严重错误
  - 作用是把“偶发失败”和“持续故障”区分开

真正的额度阈值不是写在 `alerts` 里，而是：

- 全局默认值写在 `defaults.thresholds`
- 用户个人值通过 `/配置 警告|危险|严重 <amount>` 修改

## Key 展示与隐私

- 存储层允许保存 Key 原文
- 所有对外消息只显示脱敏值
- 脱敏规则：
  - 优先保留前 17 位和后 7 位
  - 中间替换为 `.....`
  - 较短 Key 自动退化为“前 6 + 后 4”

示例：

```text
or-v1-ef1fc08d123.....7b8fd6b
```

## OpenRouter 接口

当前机器人会调用两个只读接口：

- `GET /api/v1/key`
  - 查看当前 Key 的详细信息
  - 官方文档：https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key

- `GET /api/v1/credits`
  - 查看当前认证主体的 credits 信息
  - 官方文档：https://openrouter.ai/docs/api/api-reference/credits/get-credits

说明：

- `limit_remaining` 是当前 Key 的剩余额度
- `total_credits - total_usage` 是账户级剩余额度
- 两者不是同一个概念
- 某些 Key 可能没有权限访问 `/credits`，这时 `/详细` 会在该 Key 分段里直接显示中文错误信息

## 命令行调试

### 立即执行一次阈值扫描

```powershell
.\.venv\Scripts\python.exe -m openrouter_monitor --config config.yaml --once
```

### 输出某个用户的 `/详细` 报告

```powershell
.\.venv\Scripts\python.exe -m openrouter_monitor --config config.yaml --inspect --user-open-id ou_xxxxx
```

如果当前本地只存在一个已绑定用户，可以省略 `--user-open-id`。

### 发送一条主动私聊测试消息

```powershell
.\.venv\Scripts\python.exe -m openrouter_monitor --config config.yaml --push-text "主动消息测试" --user-open-id ou_xxxxx
```

这个命令用于验证主动消息链路是否可用。

### 立即主动推送一次 `/详细` 给指定用户

```powershell
.\.venv\Scripts\python.exe -m openrouter_monitor --config config.yaml --push-detail --user-open-id ou_xxxxx
```

这个命令会立即向指定用户私聊发送一次 `/详细` 完整报告，仅用于测试，不会写入每日推送状态，也不会影响正常定时推送。

### 立即主动推送一次 `/详细` 给全部已绑定用户

```powershell
.\.venv\Scripts\python.exe -m openrouter_monitor --config config.yaml --push-detail --all-users
```

这个命令会遍历当前所有已绑定用户，逐个私聊发送 `/详细` 完整报告。同样只用于测试，不会阻碍后续定时推送。

## 本地开发

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

运行编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall openrouter_monitor tests
```

## Docker

推荐直接使用 Compose：

```powershell
docker compose up --build
```

后台运行：

```powershell
docker compose up -d --build
```

停止：

```powershell
docker compose down
```

当前仓库已提供 [docker-compose.yml](./docker-compose.yml)，默认会自动：

- 构建 `openrouter-monitor` 镜像
- 挂载 `./config.yaml` 到 `/app/config.yaml`
- 挂载 `./data` 到 `/app/data`

如果你仍然想手动用 `docker run`，再用下面这组命令。

构建镜像：

```powershell
docker build -t openrouter-monitor .
```

运行容器：

```powershell
docker run --rm `
  -v ${PWD}\config.yaml:/app/config.yaml `
  -v ${PWD}\data:/app/data `
  openrouter-monitor
```

建议挂载 `data/`，否则用户绑定关系和告警去重状态会在容器重启后丢失。

## License

本项目基于 [MIT License](./LICENSE) 开源。
