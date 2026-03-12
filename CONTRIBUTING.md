# Contributing

感谢你考虑为 `openrouter-monitor` 做贡献。

## 开发环境

本项目要求本地开发统一使用仓库内虚拟环境 `.venv`，不要使用系统全局 Python。

### 安装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

## 提交前检查

请至少执行以下命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall openrouter_monitor tests
```

## 代码约定

- 保持实现简单，可维护优先
- 新增配置项时同步更新 `config.example.yaml` 和 `README.md`
- 修改行为时同步更新测试
- 不要把真实 API Key、飞书 App Secret 或会话 ID 提交到仓库

## Pull Request 建议

- PR 标题清楚说明变更目的
- 描述里写明用户可见行为变化
- 如果改动涉及通知文案或配置结构，请附示例
- 如果改动会影响兼容性，请明确标注
