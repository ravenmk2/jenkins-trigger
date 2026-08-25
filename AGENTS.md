# AGENTS.md

## 项目简介

jenkins-trigger: 监听 GitLab Push Webhook, 按优先级编排触发 Jenkins Job, 钉钉汇总通知。
架构与驱动流程详见 `docs/architecture.md`。

## 技术栈与结构

- Python >= 3.14, FastAPI, loguru, asyncio, httpx(异步), pydantic v2, tomllib
- 源码在 `src/jenkins_trigger/`, 测试在 `tests/`, 配置示例在 `example/`, 运行时配置目录 `config/`(gitignore)
- 双端口: Webhook 8081 (`webhook.py`), API 8080 (`api.py`), 由 `main.py` 同进程启动

## 常用命令

```bash
uv sync                 # 安装依赖
uv run pytest           # 测试 (pytest-asyncio auto 模式)
uv run python -m jenkins_trigger   # 本地运行, 需先准备 config/
```

## 约定

- 依赖用 uv 管理, 改依赖后更新 `uv.lock` (`uv lock` / `uv sync`)
- 新增配置字段改 `config.py` 的 pydantic 模型, 并同步 `example/jobs/example.toml` 与文档
- Jenkins/钉钉的 HTTP 调用只走 `jenkins.py` / `dingtalk.py`, 不散落各处
- 任务状态只在内存, 不引入持久化
- 代码注释用中文, 与现有风格一致
