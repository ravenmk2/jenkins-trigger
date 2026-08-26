# 架构设计

## 概述

jenkins-trigger 支持两种触发模式: **cron 定时**(每任务可配多个表达式, 到点立即执行)与
**GitLab Push Webhook**(匹配仓库+分支, 经过去抖延迟执行)。触发后按统一流程执行:
生成执行计划 → 钉钉"开始"通知 → 按 stage 分层编排触发 Jenkins Job → 钉钉"结束"汇总通知。

## 模块划分

```
src/jenkins_trigger/
├── config.py      配置模型与加载 (TOML + pydantic 校验)
├── webhook.py     GitLab Webhook 接收端, 端口 8081
├── api.py         常规 API 端 (预留), 端口 8080
├── executor.py    核心: 事件队列、状态机、调度、执行编排、启动基线补齐
├── planner.py     执行计划生成 (Retry / Changed / Pushed)
├── cron.py        cron 定时触发 (croniter)
├── store.py       运行数据持久化 (data/jobs/<job-id>.json, 原子写)
├── jenkins.py     Jenkins API 异步客户端
├── gitlab.py      GitLab API 异步客户端 (分支 commit 查询)
├── dingtalk.py    钉钉机器人客户端
└── main.py        入口: 同进程启动两个 HTTP 服务、cron 调度与执行器
```

## 配置模型

- `config/gitlab.toml` — `[[instances]]`: id, name, url, webhook_token,
  api_token (GitLab API Token, 可空; 空则跳过该实例的 Changed 检测与基线补齐)
- `config/jenkins.toml` — `[[instances]]`: id, name, url, username, token (API Token)
- `config/dingtalk.toml` — `[[bots]]`: id, name, token, secret (加签密钥, 可空)
- `config/jobs/<job-id>.toml` — 文件名即任务 ID:
  - `name`, `gitlab`, `jenkins`, `dingtalk`(可选), `delay`(秒, 仅 Push 去抖), `default_branch`
  - `crons` — cron 表达式列表(服务器本地时区), 到点立即执行; 可空
  - `[[items]]`: `name`(必填, 钉钉通知显示名), `repo`(带 group 的仓库路径),
    `job`(Jenkins Job 路径), `branch`(覆盖默认), `stage`(执行分层, 值越小越优先, 同值并行),
    `parameterized`(bool, 默认 `false`), `build_params`(构建参数, 分支需显式写)

配置在启动时加载; 任务引用的实例 ID 不存在、cron 表达式非法、item 缺 name 都会直接报错。

## 驱动流程

```
启动: 加载配置 → 补齐 commit 基线(见下) → 启动 worker/dispatcher/cron/HTTP
                           │
cron 到点 ──→ mark_scheduled (execute_at = now)
GitLab Push ──HTTP──> webhook.py (路径定位实例, 校验 X-Gitlab-Token)
                           │  解析 仓库+分支
                           ▼
                     asyncio.Queue (内存队列)
                           │
                           ▼
              Executor._worker: 匹配任务, mark_pending
              execute_at = now + delay (重复 Push 刷新, 即去抖)
                           │
                           ▼
              Executor._dispatcher (1s 轮询): execute_at 到期 → 计划中
                           │
                           ▼
              Planner.make_plan 生成执行计划 (空计划 → 回空闲, 不通知)
                ① Changed: GitLab 分支 commit ≠ 记录 (无记录仅写基线)
                ② Retry:   Jenkins lastBuild 为 FAILURE/ABORTED
                ③ Pushed:  去抖窗口内 Push 标记的 (仓库, 分支)
                reason 优先级 Pushed > Changed > Retry, (repo, job) 去重
                           │
                           ▼
              钉钉 "Jenkins 构建开始" (列出 name + reason)
                           │
                           ▼
              执行中: 按 stage 分组, 组间串行, 组内并行
              每个执行项: 触发 → 等 queue 分配 build → 轮询至完成
              构建正常结束 → 回写 commit 记录 (异常不回写, 下轮 Changed 重试)
                           │
                           ▼
              钉钉 "Jenkins 构建结束" (✅/❌ + name + 构建号)
              通知均携带计划 ID (plan_id)
```

## 关键设计

- **任务状态机**: `空闲 → 待执行 → 计划中 → 执行中 → 空闲`。
- **去抖**: 仅 Push 触发; 延迟窗口内的再次 Push 只刷新 `execute_at`, 不重复入队。
  cron 触发不走延迟, 到点即执行。
- **并发语义**: 同一 Job 任何时刻只有一轮执行; 计划中/执行中收到新触发 →
  重新标记待执行(triggers 累积), 当前轮结束后由 dispatcher 接力拉起第二轮。
  不同 Job 之间完全并行(各自独立 asyncio task)。
- **计划 ID**: `YYMMDD-HHMMSS`(两位年, 其余补 0, 本地时间), 标记待执行时生成,
  一轮一个; 日志与钉钉通知均携带。
- **失败重跑**: Jenkins lastBuild 为 `FAILURE`/`ABORTED` 的执行项进入计划 (Retry)。
- **Changed 检测**: 依赖 GitLab api_token 查询分支 commit 与记录比较;
  未配 token 的实例整步跳过(启动时告警)。
- **commit 记录**: 触发的构建正常结束(拿到 result)即回写; 触发/轮询异常不回写,
  下轮计划以 Changed 自动重试; FAILURE/ABORTED 由 Retry 兜底。
- **启动基线补齐**: 启动时为所有 Job 所有 item 补录缺失的 commit 记录
  (已有记录保留), 之后才受理触发; 单条失败仅告警不阻塞。
- **持久化**: 分支 commit 记录按 Job 分文件存于 `data/jobs/<job-id>.json`,
  结构为 `commits → repo → branch → sha` 三级嵌套; 先写 tmp 再 `os.replace` 原子落盘;
  文件损坏按空记录处理(启动补齐会重建基线)。其余任务状态仍在内存。
- **同仓库多 Job**: 同一 `repo` 可出现在多个执行项中(绑定不同 Jenkins Job),
  全部进入计划; 执行计划按 `(repo, job)` 去重。
- **stage 分层编排**: 轻量 DAG 表达——`stage` 值越小越早执行; 同 stage 的执行项用 `asyncio.gather` 并行。
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
| 8081 | `POST /webhook/gitlab/{instance-id}` | GitLab Webhook, 路径定位实例, 校验 `X-Gitlab-Token`; 按 payload 的 `object_kind` 判断, 支持 Push Hook 与 System Hook |
| 8080 | `GET /api/health` | 健康检查 |
| 8080 | `GET /api/status` | 所有任务状态与最近一次执行结果 |

## 部署

- Docker 镜像不含 `config/`, 运行时挂载配置目录(密钥不进镜像)。
- `VOLUME /app/data`: 运行数据(commit 记录)持久化; 建议显式 `-v` 挂载,
  未挂载时 docker 自动创建匿名卷。镜像内 `/app/data` 已 chown 给运行用户 (uid 10001)。
- GitHub Actions: push 到 master/develop → 测试 + 推送 `dev` 标签镜像到 ghcr.io;
  push `v*` tag → 推送对应版本标签。镜像带标准 OCI 标签。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JENKINS_TRIGGER_CONFIG` | `config` | 配置目录 |
| `JENKINS_TRIGGER_DATA` | `data` | 运行数据目录 (commit 记录) |
| `WEBHOOK_PORT` / `API_PORT` | `8081` / `8080` | 监听端口 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
