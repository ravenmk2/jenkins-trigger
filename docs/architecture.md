# 架构设计

## 概述

jenkins-trigger 监听 GitLab Push Webhook 事件, 按配置匹配任务, 经过去抖延迟后按优先级编排触发 Jenkins Job, 等待所有构建完成后通过钉钉机器人汇总通知结果。

## 模块划分

```
src/jenkins_trigger/
├── config.py      配置模型与加载 (TOML + pydantic 校验)
├── webhook.py     GitLab Webhook 接收端, 端口 8081
├── api.py         常规 API 端 (预留), 端口 8080
├── executor.py    核心: 事件队列、状态机、调度、执行编排
├── jenkins.py     Jenkins API 异步客户端
├── dingtalk.py    钉钉机器人客户端
└── main.py        入口: 同进程启动两个 HTTP 服务与执行器
```

## 配置模型

- `config/gitlab.toml` — `[[instances]]`: id, name, url, webhook_token
- `config/jenkins.toml` — `[[instances]]`: id, name, url, username, token (API Token)
- `config/dingtalk.toml` — `[[bots]]`: id, name, token, secret (加签密钥, 可空)
- `config/jobs/<job-id>.toml` — 文件名即任务 ID:
  - `name`, `gitlab`, `jenkins`, `dingtalk`(可选), `delay`(秒), `default_branch`
  - `[[items]]`: `repo`(带 group 的仓库路径), `job`(Jenkins Job 路径), `branch`(覆盖默认),
    `priority`(值越小越优先, 同值并行), `parameterized`(bool, 默认 `false`), `build_params`(构建参数, 分支需显式写)

配置在启动时加载; 任务引用的实例 ID 不存在会直接报错。

## 驱动流程

```
GitLab Push ──HTTP──> webhook.py (路径定位实例, 校验 X-Gitlab-Token)
                           │  解析 仓库+分支
                           ▼
                     asyncio.Queue (内存队列)
                           │
                           ▼
              Executor._worker: 匹配任务, 标记 待执行
              execute_at = now + delay (重复 Push 刷新, 即去抖)
                           │
                           ▼
              Executor._scheduler (1s 轮询): execute_at 到期 → 执行中
                           │
                           ▼
              _make_plan: 匹配分支的执行项
                        ∪ 任务内上次构建 FAILED/ABORTED 的执行项
                           │
                           ▼
              _execute_plan: 按 priority 分组, 组间串行, 组内并行
              每个执行项: 触发 → 等 queue 分配 build → 轮询至完成
                           │
                           ▼
              钉钉 Markdown 汇总通知 (任务未配置机器人则跳过)
```

## 关键设计

- **任务状态机**: `空闲 → 待执行 → 执行中 → 空闲`。状态仅存内存, 重启后丢失待执行任务。
- **去抖**: 延迟窗口内的再次 Push 只刷新 `execute_at`, 不重复入队。
- **执行中再收到 Push**: 重新标记为待执行, 当前执行完成后由调度器再次拉起。
- **失败重跑**: 执行前查询任务内所有 Jenkins Job 的 `lastBuild`, 结果为
  `FAILURE`/`ABORTED` 的仓库一并加入本次计划(按各自配置的分支触发)。
- **同仓库多 Job**: 同一 `repo` 可出现在多个执行项中(绑定不同 Jenkins Job),
  全部进入计划; 执行计划按 `(repo, job)` 去重, 完全相同的重复项只执行一次。
- **优先级编排**: `priority` 值越小越早执行; 同值的仓库用 `asyncio.gather` 并行。
- **Jenkins 触发方式**(`parameterized`):
  - `true`: `POST /job/<path>/buildWithParameters?<build_params>` (参数原样透传,
    分支需要时在 `build_params` 显式写; 构建分支恒等于仓库配置的生效分支, 是静态值)
  - `false`: `POST /job/<path>/build` (纯触发, 不带参数; 配置 `build_params` 会启动报错)
  - 自动获取 CSRF crumb (`/crumbIssuer/api/json`), 未启用防护时跳过。
- **双端口**: Webhook (8081) 与 API (8080) 是两个独立的 uvicorn Server,
  同进程 asyncio 运行; API 端可仅暴露在内网。
- **Webhook 处理**: 先校验 Token 并入内存队列立即返回, 匹配与执行异步进行,
  避免 GitLab 侧超时重发。

## 端口与端点

| 端口 | 端点 | 说明 |
| --- | --- | --- |
| 8081 | `POST /webhook/gitlab/{instance-id}` | GitLab Webhook, 路径定位实例, 校验 `X-Gitlab-Token` |
| 8080 | `GET /api/health` | 健康检查 |
| 8080 | `GET /api/status` | 所有任务状态与最近一次执行结果 |

## 部署

- Docker 镜像不含 `config/`, 运行时挂载配置目录(密钥不进镜像)。
- GitHub Actions: push 到 master/develop → 测试 + 推送 `dev` 标签镜像到 ghcr.io;
  push `v*` tag → 推送对应版本标签。镜像带标准 OCI 标签。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JENKINS_TRIGGER_CONFIG` | `config` | 配置目录 |
| `WEBHOOK_PORT` / `API_PORT` | `8081` / `8080` | 监听端口 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
